from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.request import BudgetConstraint
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.planning.material_contracts import (
    BudgetAllocationStatus,
    PlanningMaterialStatus,
    RouteMatrixStatus,
)


class RouteFailureInjection(StrEnum):
    NONE = "none"
    ONE_TIMEOUT = "one_timeout"


class PlanningMaterialExpectation(DomainModel):
    material_status: PlanningMaterialStatus
    route_status: RouteMatrixStatus
    budget_status: BudgetAllocationStatus
    expected_edge_count: int = Field(ge=0, le=20)
    expected_failed_edge_count: int = Field(ge=0, le=20)
    expected_route_provider_calls: int = Field(ge=0, le=20)

    @model_validator(mode="after")
    def validate_counts(self) -> "PlanningMaterialExpectation":
        if self.expected_failed_edge_count > self.expected_edge_count:
            raise ValueError("expected failed routes cannot exceed expected routes")
        if self.expected_route_provider_calls != self.expected_edge_count:
            raise ValueError("every expected route edge requires one Provider call")
        return self


class PlanningMaterialEvalCase(DomainModel):
    case_id: Identifier
    title: NonEmptyText
    specialist_case_id: Identifier
    request_budget: BudgetConstraint | None = None
    preserve_source_budget: bool = False
    route_failure: RouteFailureInjection = RouteFailureInjection.NONE
    expected: PlanningMaterialExpectation
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case(self) -> "PlanningMaterialEvalCase":
        if not self.case_id.startswith("planning-materials-") or not self.case_id.endswith("-v1"):
            raise ValueError("planning material case_id must encode the suite and version")
        if self.request_budget is not None and self.preserve_source_budget:
            raise ValueError("a case cannot both override and preserve the source budget")
        expects_failure = self.expected.expected_failed_edge_count == 1
        if (self.route_failure == RouteFailureInjection.ONE_TIMEOUT) != expects_failure:
            raise ValueError("route failure injection must match the expected failed edge count")
        return self


class PlanningMaterialEvalSuite(DomainModel):
    suite: Literal["planning-materials-v1"] = "planning-materials-v1"
    version: Literal[1] = 1
    data_mode: Literal["fixture"] = "fixture"
    cases: tuple[PlanningMaterialEvalCase, ...] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def validate_inventory(self) -> "PlanningMaterialEvalSuite":
        case_ids = [item.case_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("planning material case ids must be unique")
        statuses = [item.expected.material_status for item in self.cases]
        if statuses.count(PlanningMaterialStatus.READY) != 1:
            raise ValueError("planning material V1 requires one ready control case")
        if statuses.count(PlanningMaterialStatus.PARTIAL) != 3:
            raise ValueError("planning material V1 requires three partial cases")
        if statuses.count(PlanningMaterialStatus.BLOCKED) != 1:
            raise ValueError("planning material V1 requires one blocked case")
        if sum(item.route_failure == RouteFailureInjection.ONE_TIMEOUT for item in self.cases) != 1:
            raise ValueError("planning material V1 requires one route timeout injection")
        return self


class PlanningMaterialCaseResult(DomainModel):
    case_id: Identifier
    expected_material_status: PlanningMaterialStatus
    actual_material_status: PlanningMaterialStatus | None = None
    passed: bool
    expected_edge_count: int = Field(ge=0, le=20)
    actual_edge_count: int = Field(ge=0, le=20)
    failed_edge_count: int = Field(ge=0, le=20)
    typed_route_failure_count: int = Field(ge=0, le=20)
    route_provider_call_count: int = Field(ge=0, le=20)
    route_provider_peak: int = Field(ge=0, le=8)
    source_traceability_passed: bool
    budget_sum_exact: bool
    budget_target_total: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    error_code: Identifier | None = None
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> "PlanningMaterialCaseResult":
        if self.passed != all(item.passed for item in self.checks):
            raise ValueError("planning material case passed must equal its check conjunction")
        if self.typed_route_failure_count > self.failed_edge_count:
            raise ValueError("typed route failures cannot exceed failed edges")
        return self


class PlanningMaterialBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["planning-materials-v1"] = "planning-materials-v1"
    bundle_version: Literal["planning-materials-v1"] = "planning-materials-v1"
    route_matrix_version: Literal["route-matrix-v1"] = "route-matrix-v1"
    budget_allocator_version: Literal["budget-allocator-v1"] = "budget-allocator-v1"
    execution_mode: Literal["fixture"] = "fixture"
    dataset_sha256: Sha256Digest
    case_count: Literal[5] = 5
    passed_case_count: int = Field(ge=0, le=5)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    expected_edge_count: int = Field(ge=0, le=100)
    actual_edge_count: int = Field(ge=0, le=100)
    route_provider_call_count: int = Field(ge=0, le=100)
    typed_route_failure_count: int = Field(ge=0, le=20)
    exact_budget_case_count: int = Field(ge=0, le=5)
    blocked_zero_route_call_case_count: int = Field(ge=0, le=1)
    bounded_concurrency_case_count: int = Field(ge=0, le=5)
    source_traceability_case_count: int = Field(ge=0, le=5)
    results: tuple[PlanningMaterialCaseResult, ...] = Field(min_length=5, max_length=5)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aggregates(self) -> "PlanningMaterialBaselineReport":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("planning material report case ids must be unique")
        aggregates = {
            "passed_case_count": sum(item.passed for item in self.results),
            "expected_edge_count": sum(item.expected_edge_count for item in self.results),
            "actual_edge_count": sum(item.actual_edge_count for item in self.results),
            "route_provider_call_count": sum(
                item.route_provider_call_count for item in self.results
            ),
            "typed_route_failure_count": sum(
                item.typed_route_failure_count for item in self.results
            ),
            "exact_budget_case_count": sum(item.budget_sum_exact for item in self.results),
            "bounded_concurrency_case_count": sum(
                item.route_provider_peak <= 4 for item in self.results
            ),
            "source_traceability_case_count": sum(
                item.source_traceability_passed for item in self.results
            ),
        }
        for field_name, expected in aggregates.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name} must match planning material case results")
        blocked_zero_calls = sum(
            item.actual_material_status == PlanningMaterialStatus.BLOCKED
            and item.route_provider_call_count == 0
            for item in self.results
        )
        if self.blocked_zero_route_call_case_count != blocked_zero_calls:
            raise ValueError("blocked zero-call count must match planning material results")
        if self.case_pass_rate != expected_rate(self.passed_case_count, self.case_count):
            raise ValueError("case pass rate must match planning material results")
        return self
