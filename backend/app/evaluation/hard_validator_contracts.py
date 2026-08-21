from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.validation import (
    IssueSeverity,
    PlanValidationStatus,
    RepairAction,
    ResponsibleNode,
)
from app.evaluation.contracts import EvaluationCheck, expected_rate


class HardConstraintScenario(StrEnum):
    PRESERVE = "preserve"
    MISSING_MUST_VISIT = "missing_must_visit"
    SCHEDULED_AVOID = "scheduled_avoid"
    HARD_BUDGET = "hard_budget"


class HardPlanMutation(StrEnum):
    NONE = "none"
    MISSING_ROUTE = "missing_route"
    TIGHT_TRANSFER = "tight_transfer"
    CANDIDATE_SOURCE_MISMATCH = "candidate_source_mismatch"
    ROUTE_SOURCE_MISMATCH = "route_source_mismatch"


class HardMaterialMutation(StrEnum):
    NONE = "none"
    POI_CROSS_CITY = "poi_cross_city"
    STAY_CROSS_CITY = "stay_cross_city"


class OpeningEvidenceScenario(StrEnum):
    COMPLETE = "complete"
    MISSING_ONE = "missing_one"
    OUTSIDE_ONE = "outside_one"


class ExpectedValidationIssue(DomainModel):
    rule_code: NonEmptyText
    severity: IssueSeverity
    responsible_node: ResponsibleNode
    repair_action: RepairAction


class HardValidatorExpectation(DomainModel):
    status: PlanValidationStatus
    can_finalize: bool
    issues: tuple[ExpectedValidationIssue, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_issues(self) -> "HardValidatorExpectation":
        if len({item.rule_code for item in self.issues}) != len(self.issues):
            raise ValueError("expected hard-validator issue rule codes must be unique")
        has_error = any(item.severity == IssueSeverity.ERROR for item in self.issues)
        has_warning = any(item.severity == IssueSeverity.WARNING for item in self.issues)
        expected_status = PlanValidationStatus.PASSED
        if has_error:
            expected_status = PlanValidationStatus.CONFLICTED
        elif has_warning:
            expected_status = PlanValidationStatus.WARNING
        if self.status != expected_status or self.can_finalize == has_error:
            raise ValueError("expected status and finalization must match issue severities")
        return self


class HardValidatorEvalCase(DomainModel):
    case_id: Identifier
    title: NonEmptyText
    source_plan_case_id: Identifier
    constraint_scenario: HardConstraintScenario = HardConstraintScenario.PRESERVE
    plan_mutation: HardPlanMutation = HardPlanMutation.NONE
    material_mutation: HardMaterialMutation = HardMaterialMutation.NONE
    opening_evidence: OpeningEvidenceScenario = OpeningEvidenceScenario.COMPLETE
    expected: HardValidatorExpectation
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)


class HardValidatorEvalSuite(DomainModel):
    suite: Literal["hard-validator-v1"] = "hard-validator-v1"
    version: Literal[1] = 1
    cases: tuple[HardValidatorEvalCase, ...] = Field(min_length=12, max_length=12)

    @model_validator(mode="after")
    def validate_inventory(self) -> "HardValidatorEvalSuite":
        case_ids = [item.case_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("hard-validator case ids must be unique")
        return self


class HardValidatorCaseResult(DomainModel):
    case_id: Identifier
    passed: bool
    expected_status: PlanValidationStatus
    actual_status: PlanValidationStatus
    expected_can_finalize: bool
    actual_can_finalize: bool
    expected_issues: tuple[ExpectedValidationIssue, ...]
    actual_issues: tuple[ExpectedValidationIssue, ...]
    routing_match_count: int = Field(ge=0)
    deterministic_replay: bool
    validator_model_call_count: Literal[0] = 0
    error_code: Identifier | None = None
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> "HardValidatorCaseResult":
        if self.routing_match_count > len(self.expected_issues):
            raise ValueError("routing matches cannot exceed expected issues")
        if self.passed != all(item.passed for item in self.checks):
            raise ValueError("hard-validator case passed must equal all checks")
        if (self.error_code is None) != self.checks[0].passed:
            raise ValueError("the first check must represent workflow completion")
        return self


class HardValidatorBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["hard-validator-v1"] = "hard-validator-v1"
    validator_version: Literal["hard-trip-plan-validator-v1"] = "hard-trip-plan-validator-v1"
    execution_mode: Literal["fixture"] = "fixture"
    dataset_sha256: Sha256Digest
    case_count: Literal[12] = 12
    passed_case_count: int = Field(ge=0, le=12)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    exact_issue_set_case_count: int = Field(ge=0, le=12)
    exact_issue_set_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    expected_issue_count: int = Field(ge=0)
    routing_match_count: int = Field(ge=0)
    routing_accuracy: Decimal = Field(ge=0, le=1, decimal_places=4)
    deterministic_replay_case_count: int = Field(ge=0, le=12)
    validator_model_call_count: Literal[0] = 0
    results: tuple[HardValidatorCaseResult, ...] = Field(min_length=12, max_length=12)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aggregates(self) -> "HardValidatorBaselineReport":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("hard-validator report case ids must be unique")
        expected_issue_set_cases = sum(
            {item.rule_code for item in result.expected_issues}
            == {item.rule_code for item in result.actual_issues}
            for result in self.results
        )
        aggregates = {
            "passed_case_count": sum(item.passed for item in self.results),
            "exact_issue_set_case_count": expected_issue_set_cases,
            "expected_issue_count": sum(len(item.expected_issues) for item in self.results),
            "routing_match_count": sum(item.routing_match_count for item in self.results),
            "deterministic_replay_case_count": sum(
                item.deterministic_replay for item in self.results
            ),
            "validator_model_call_count": sum(
                item.validator_model_call_count for item in self.results
            ),
        }
        for field_name, aggregate_expected in aggregates.items():
            if getattr(self, field_name) != aggregate_expected:
                raise ValueError(f"{field_name} must match hard-validator case results")
        rates = {
            "case_pass_rate": expected_rate(self.passed_case_count, self.case_count),
            "exact_issue_set_rate": expected_rate(self.exact_issue_set_case_count, self.case_count),
            "routing_accuracy": expected_rate(self.routing_match_count, self.expected_issue_count),
        }
        for field_name, rate_expected in rates.items():
            if getattr(self, field_name) != rate_expected:
                raise ValueError(f"{field_name} must match hard-validator aggregate counts")
        return self
