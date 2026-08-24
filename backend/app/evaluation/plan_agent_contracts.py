from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.agents.contracts import ModelTokenUsage
from app.agents.plan_agent_contracts import PlanAgentRunStatus
from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.request import TripRequest
from app.domain.validation import PlanValidationStatus
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.planning_materials_contracts import RouteFailureInjection
from app.planning.material_contracts import PlanningMaterialStatus


class PlanAgentExpectation(DomainModel):
    material_status: PlanningMaterialStatus
    run_status: PlanAgentRunStatus
    candidate_count: int = Field(ge=0, le=4)
    model_call_count: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_routing(self) -> "PlanAgentExpectation":
        should_plan = self.material_status != PlanningMaterialStatus.BLOCKED
        if should_plan != (self.run_status == PlanAgentRunStatus.PLANNED):
            raise ValueError("ready and partial materials must expect a planned result")
        if self.model_call_count != int(should_plan):
            raise ValueError("expected model calls must follow material readiness")
        if should_plan and self.candidate_count == 0:
            raise ValueError("planned cases require at least one input candidate")
        return self


class PlanAgentEvalCase(DomainModel):
    case_id: Identifier
    title: NonEmptyText
    request: TripRequest
    explore_fixture_case_id: Identifier
    stay_fixture_case_id: Identifier
    route_failure: RouteFailureInjection = RouteFailureInjection.NONE
    expected: PlanAgentExpectation
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case(self) -> "PlanAgentEvalCase":
        if not self.case_id.startswith("plan-agent-") or not self.case_id.endswith("-v1"):
            raise ValueError("Plan Agent case_id must encode the suite and version")
        if self.expected.run_status == PlanAgentRunStatus.PLANNED and (
            self.request.budget is None or self.request.budget.hard_limit
        ):
            raise ValueError("planned eval cases require a soft budget truth boundary")
        if self.route_failure == RouteFailureInjection.ONE_TIMEOUT and (
            self.expected.material_status != PlanningMaterialStatus.PARTIAL
        ):
            raise ValueError("route timeout cases must expect partial materials")
        return self


class PlanAgentEvalSuite(DomainModel):
    suite: Literal["plan-agent-v1"] = "plan-agent-v1"
    version: Literal[1] = 1
    data_mode: Literal["fixture"] = "fixture"
    cases: tuple[PlanAgentEvalCase, ...] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def validate_inventory(self) -> "PlanAgentEvalSuite":
        case_ids = tuple(item.case_id for item in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Plan Agent case ids must be unique")
        planned = sum(item.expected.run_status == PlanAgentRunStatus.PLANNED for item in self.cases)
        skipped = len(self.cases) - planned
        if (planned, skipped) != (5, 1):
            raise ValueError("Plan Agent V1 requires five planned and one blocked case")
        if sum(item.route_failure == RouteFailureInjection.ONE_TIMEOUT for item in self.cases) != 1:
            raise ValueError("Plan Agent V1 requires one route timeout case")
        if (
            sum(
                item.expected.material_status == PlanningMaterialStatus.BLOCKED
                for item in self.cases
            )
            != 1
        ):
            raise ValueError("Plan Agent V1 requires one blocked capability case")
        return self


class PlanAgentCaseResult(DomainModel):
    case_id: Identifier
    passed: bool
    expected_material_status: PlanningMaterialStatus
    actual_material_status: PlanningMaterialStatus | None = None
    expected_run_status: PlanAgentRunStatus
    actual_run_status: PlanAgentRunStatus | None = None
    candidate_count: int = Field(ge=0, le=4)
    scheduled_candidate_count: int = Field(ge=0, le=4)
    grounded_candidate_count: int = Field(ge=0, le=4)
    traceable_candidate_count: int = Field(ge=0, le=4)
    route_backed_candidate_count: int = Field(ge=0, le=4)
    input_weather_risk_count: int = Field(ge=0, le=5)
    preserved_weather_risk_count: int = Field(ge=0, le=5)
    day_count: int = Field(ge=0, le=5)
    cost_item_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0, le=1)
    validation_status: PlanValidationStatus | None = None
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None
    error_code: Identifier | None = None
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> "PlanAgentCaseResult":
        if self.passed != all(item.passed for item in self.checks):
            raise ValueError("Plan Agent case passed must equal its check conjunction")
        if not (
            self.route_backed_candidate_count
            <= self.traceable_candidate_count
            <= self.grounded_candidate_count
            <= self.scheduled_candidate_count
            <= self.candidate_count
        ):
            raise ValueError("Plan Agent candidate evidence counts must be monotonic")
        if self.preserved_weather_risk_count > self.input_weather_risk_count:
            raise ValueError("preserved weather risks cannot exceed input risks")
        return self


class PlanAgentBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["plan-agent-v1"] = "plan-agent-v1"
    agent_version: Literal["multi-agent-plan-v1"] = "multi-agent-plan-v1"
    execution_mode: Literal["fixture", "live"]
    model: NonEmptyText
    dataset_sha256: Sha256Digest
    case_count: Literal[6] = 6
    passed_case_count: int = Field(ge=0, le=6)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    planned_case_count: int = Field(ge=0, le=5)
    skipped_case_count: int = Field(ge=0, le=1)
    model_call_count: int = Field(ge=0, le=5)
    candidate_count: int = Field(ge=0, le=16)
    scheduled_candidate_count: int = Field(ge=0, le=16)
    grounding_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    source_traceability_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    route_lineage_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    weather_preservation_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    zero_cost_claim_case_count: int = Field(ge=0, le=5)
    skipped_zero_model_call_case_count: int = Field(ge=0, le=1)
    usage_case_count: int = Field(ge=0, le=5)
    total_prompt_tokens: int = Field(ge=0)
    total_completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    p50_latency_ms: int = Field(ge=0)
    p95_latency_ms: int = Field(ge=0)
    results: tuple[PlanAgentCaseResult, ...] = Field(min_length=6, max_length=6)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aggregates(self) -> "PlanAgentBaselineReport":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("Plan Agent report case ids must be unique")
        planned = tuple(
            item for item in self.results if item.actual_run_status == PlanAgentRunStatus.PLANNED
        )
        skipped = tuple(
            item for item in self.results if item.actual_run_status == PlanAgentRunStatus.SKIPPED
        )
        aggregates = {
            "passed_case_count": sum(item.passed for item in self.results),
            "planned_case_count": len(planned),
            "skipped_case_count": len(skipped),
            "model_call_count": sum(item.model_call_count for item in self.results),
            "candidate_count": sum(item.candidate_count for item in planned),
            "scheduled_candidate_count": sum(item.scheduled_candidate_count for item in planned),
            "zero_cost_claim_case_count": sum(item.cost_item_count == 0 for item in planned),
            "skipped_zero_model_call_case_count": sum(
                item.model_call_count == 0 for item in skipped
            ),
            "usage_case_count": sum(item.usage is not None for item in self.results),
            "total_prompt_tokens": sum(
                item.usage.prompt_tokens for item in self.results if item.usage is not None
            ),
            "total_completion_tokens": sum(
                item.usage.completion_tokens for item in self.results if item.usage is not None
            ),
            "total_tokens": sum(
                item.usage.total_tokens for item in self.results if item.usage is not None
            ),
        }
        for field_name, expected in aggregates.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name} must match Plan Agent case results")
        evidence_counts = {
            "grounding_rate": sum(item.grounded_candidate_count for item in planned),
            "source_traceability_rate": sum(item.traceable_candidate_count for item in planned),
            "route_lineage_rate": sum(item.route_backed_candidate_count for item in planned),
        }
        for field_name, numerator in evidence_counts.items():
            if getattr(self, field_name) != expected_rate(
                numerator, self.scheduled_candidate_count
            ):
                raise ValueError(f"{field_name} must match Plan Agent evidence counts")
        weather_input = sum(item.input_weather_risk_count for item in planned)
        weather_preserved = sum(item.preserved_weather_risk_count for item in planned)
        if self.weather_preservation_rate != expected_rate(weather_preserved, weather_input):
            raise ValueError("weather preservation rate must match Plan Agent case results")
        if self.case_pass_rate != expected_rate(self.passed_case_count, self.case_count):
            raise ValueError("case pass rate must match Plan Agent case results")
        return self
