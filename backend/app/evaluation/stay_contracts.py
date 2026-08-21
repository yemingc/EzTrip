from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.agents.contracts import ModelTokenUsage
from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.candidates import GeoPoint
from app.domain.context import ClarificationKind
from app.domain.request import TripRequest
from app.evaluation.contracts import EvaluationCheck, expected_rate


class StayFixtureCandidateSpec(DomainModel):
    candidate_id: Identifier
    provider_id: NonEmptyText
    name: NonEmptyText
    city: NonEmptyText
    district: NonEmptyText | None = None
    address: NonEmptyText | None = None
    location: GeoPoint
    area_name: NonEmptyText
    tags: tuple[NonEmptyText, ...] = ()
    data_mode: Literal["fixture"] = "fixture"


class StayAgentExpectation(DomainModel):
    outcome: Literal["recommendations", "blocked"]
    required_context_refs: tuple[NonEmptyText, ...] = Field(max_length=4)
    allowed_recommendation_ids: tuple[Identifier, ...] = Field(max_length=3)
    forbidden_recommendation_ids: tuple[Identifier, ...] = Field(max_length=2)
    required_recommendation_groups: tuple[tuple[Identifier, ...], ...] = Field(max_length=2)
    expected_clarification_kind: ClarificationKind | None = None

    @model_validator(mode="after")
    def validate_outcome_contract(self) -> "StayAgentExpectation":
        allowed = set(self.allowed_recommendation_ids)
        forbidden = set(self.forbidden_recommendation_ids)
        if allowed & forbidden:
            raise ValueError("allowed and forbidden Stay candidates cannot overlap")
        if self.outcome == "recommendations":
            if not allowed or not self.required_recommendation_groups:
                raise ValueError("recommendation cases require allowed candidates and groups")
            if self.expected_clarification_kind is not None:
                raise ValueError("recommendation cases cannot expect a blocking clarification")
            if any(
                not group or not set(group).issubset(allowed)
                for group in self.required_recommendation_groups
            ):
                raise ValueError(
                    "required recommendation groups must be non-empty subsets of allowed ids"
                )
        else:
            if any(
                (
                    self.required_context_refs,
                    self.allowed_recommendation_ids,
                    self.forbidden_recommendation_ids,
                    self.required_recommendation_groups,
                )
            ):
                raise ValueError("blocked Stay cases cannot carry recommendation expectations")
            if self.expected_clarification_kind is None:
                raise ValueError("blocked Stay cases require an expected clarification kind")
        return self


class StayAgentEvalCase(DomainModel):
    case_id: Identifier
    title: NonEmptyText
    request: TripRequest
    provider_candidates: tuple[StayFixtureCandidateSpec, ...] = Field(max_length=3)
    expected: StayAgentExpectation
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_inventory(self) -> "StayAgentEvalCase":
        if not self.case_id.startswith("stay-") or not self.case_id.endswith("-v1"):
            raise ValueError("Stay case_id must encode the suite and version")
        candidate_ids = [item.candidate_id for item in self.provider_candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Stay provider candidate ids must be unique")
        if self.expected.outcome == "recommendations":
            if len(self.provider_candidates) != 3:
                raise ValueError("recommendation cases require exactly three fixture candidates")
            candidate_set = set(candidate_ids)
            labelled = set(self.expected.allowed_recommendation_ids) | set(
                self.expected.forbidden_recommendation_ids
            )
            if labelled != candidate_set:
                raise ValueError("every Stay fixture candidate must be labelled")
            if any(item.city != self.request.destination_city for item in self.provider_candidates):
                raise ValueError("Stay fixture candidate cities must match the request")
        elif self.provider_candidates:
            raise ValueError("blocked Stay cases cannot include provider candidates")
        return self


class StayAgentEvalSuite(DomainModel):
    suite: Literal["stay-agent-v1"] = "stay-agent-v1"
    version: Literal[1] = 1
    data_mode: Literal["fixture"] = "fixture"
    cases: tuple[StayAgentEvalCase, ...] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def validate_suite_inventory(self) -> "StayAgentEvalSuite":
        case_ids = [item.case_id for item in self.cases]
        request_ids = [item.request.request_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Stay evaluation case ids must be unique")
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("Stay evaluation request ids must be unique")
        if sum(item.expected.outcome == "recommendations" for item in self.cases) != 4:
            raise ValueError("Stay V1 suite requires four recommendation cases")
        if sum(item.expected.outcome == "blocked" for item in self.cases) != 2:
            raise ValueError("Stay V1 suite requires two blocked routing cases")
        return self


class StayAgentCaseResult(DomainModel):
    case_id: Identifier
    expected_outcome: Literal["recommendations", "blocked"]
    passed: bool
    model_call_count: int = Field(ge=0, le=2)
    provider_call_count: int = Field(ge=0, le=3)
    query_count: int = Field(ge=0, le=3)
    required_context_ref_count: int = Field(ge=0, le=4)
    matched_context_ref_count: int = Field(ge=0, le=4)
    recommendation_count: int = Field(ge=0, le=6)
    grounded_recommendation_count: int = Field(ge=0, le=6)
    traceable_recommendation_count: int = Field(ge=0, le=6)
    allowed_recommendation_count: int = Field(ge=0, le=6)
    required_recommendation_group_count: int = Field(ge=0, le=2)
    matched_recommendation_group_count: int = Field(ge=0, le=2)
    unverified_price_field_count: int = Field(ge=0, le=6)
    unknown_availability_count: int = Field(ge=0, le=6)
    booking_disabled_count: int = Field(ge=0, le=6)
    query_latency_ms: int = Field(ge=0)
    selection_latency_ms: int = Field(ge=0)
    query_usage: ModelTokenUsage | None = None
    selection_usage: ModelTokenUsage | None = None
    error_code: Identifier | None = None
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_result(self) -> "StayAgentCaseResult":
        if self.matched_context_ref_count > self.required_context_ref_count:
            raise ValueError("matched context refs cannot exceed required context refs")
        if self.grounded_recommendation_count > self.recommendation_count:
            raise ValueError("grounded recommendations cannot exceed recommendations")
        if self.traceable_recommendation_count > self.grounded_recommendation_count:
            raise ValueError("traceable recommendations cannot exceed grounded recommendations")
        if self.allowed_recommendation_count > self.recommendation_count:
            raise ValueError("allowed recommendations cannot exceed recommendations")
        if self.matched_recommendation_group_count > self.required_recommendation_group_count:
            raise ValueError("matched recommendation groups cannot exceed required groups")
        if self.unknown_availability_count > self.recommendation_count:
            raise ValueError("unknown availability count cannot exceed recommendations")
        if self.booking_disabled_count > self.recommendation_count:
            raise ValueError("booking-disabled count cannot exceed recommendations")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("Stay case passed must equal the conjunction of checks")
        return self


class StayAgentBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["stay-agent-v1"] = "stay-agent-v1"
    agent_version: Literal["stay-agent-v1"] = "stay-agent-v1"
    query_prompt_version: Literal["stay-query-strategy-v1"] = "stay-query-strategy-v1"
    selection_prompt_version: Literal["stay-candidate-selection-v1"] = "stay-candidate-selection-v1"
    execution_mode: Literal["fixture", "live"]
    model: NonEmptyText
    dataset_sha256: Sha256Digest
    case_count: Literal[6] = 6
    recommendation_case_count: Literal[4] = 4
    blocked_case_count: Literal[2] = 2
    passed_case_count: int = Field(ge=0, le=6)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    model_call_count: int = Field(ge=0, le=8)
    provider_call_count: int = Field(ge=0, le=12)
    required_context_ref_count: int = Field(ge=0, le=16)
    matched_context_ref_count: int = Field(ge=0, le=16)
    context_reference_coverage_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    recommendation_count: int = Field(ge=0)
    grounded_recommendation_count: int = Field(ge=0)
    grounding_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    traceable_recommendation_count: int = Field(ge=0)
    source_traceability_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    allowed_recommendation_count: int = Field(ge=0)
    labelled_relevance_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    required_recommendation_group_count: int = Field(ge=0, le=8)
    matched_recommendation_group_count: int = Field(ge=0, le=8)
    recommendation_group_coverage_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    unverified_price_field_count: int = Field(ge=0)
    unknown_availability_count: int = Field(ge=0)
    booking_disabled_count: int = Field(ge=0)
    commercial_truth_boundary_passed: bool
    usage_call_count: int = Field(ge=0, le=8)
    total_prompt_tokens: int = Field(ge=0)
    total_completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    p50_case_latency_ms: int = Field(ge=0)
    p95_case_latency_ms: int = Field(ge=0)
    results: tuple[StayAgentCaseResult, ...] = Field(min_length=6, max_length=6)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report_aggregates(self) -> "StayAgentBaselineReport":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("Stay report case ids must be unique")
        scalar_aggregates = {
            "passed_case_count": sum(item.passed for item in self.results),
            "model_call_count": sum(item.model_call_count for item in self.results),
            "provider_call_count": sum(item.provider_call_count for item in self.results),
            "required_context_ref_count": sum(
                item.required_context_ref_count for item in self.results
            ),
            "matched_context_ref_count": sum(
                item.matched_context_ref_count for item in self.results
            ),
            "recommendation_count": sum(item.recommendation_count for item in self.results),
            "grounded_recommendation_count": sum(
                item.grounded_recommendation_count for item in self.results
            ),
            "traceable_recommendation_count": sum(
                item.traceable_recommendation_count for item in self.results
            ),
            "allowed_recommendation_count": sum(
                item.allowed_recommendation_count for item in self.results
            ),
            "required_recommendation_group_count": sum(
                item.required_recommendation_group_count for item in self.results
            ),
            "matched_recommendation_group_count": sum(
                item.matched_recommendation_group_count for item in self.results
            ),
            "unverified_price_field_count": sum(
                item.unverified_price_field_count for item in self.results
            ),
            "unknown_availability_count": sum(
                item.unknown_availability_count for item in self.results
            ),
            "booking_disabled_count": sum(item.booking_disabled_count for item in self.results),
            "usage_call_count": sum(
                (item.query_usage is not None) + (item.selection_usage is not None)
                for item in self.results
            ),
            "total_prompt_tokens": sum(
                usage.prompt_tokens
                for item in self.results
                for usage in (item.query_usage, item.selection_usage)
                if usage is not None
            ),
            "total_completion_tokens": sum(
                usage.completion_tokens
                for item in self.results
                for usage in (item.query_usage, item.selection_usage)
                if usage is not None
            ),
            "total_tokens": sum(
                usage.total_tokens
                for item in self.results
                for usage in (item.query_usage, item.selection_usage)
                if usage is not None
            ),
        }
        for field_name, expected in scalar_aggregates.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name} must match Stay case results")
        rate_aggregates = {
            "case_pass_rate": expected_rate(self.passed_case_count, self.case_count),
            "context_reference_coverage_rate": expected_rate(
                self.matched_context_ref_count,
                self.required_context_ref_count,
            ),
            "grounding_rate": expected_rate(
                self.grounded_recommendation_count,
                self.recommendation_count,
            ),
            "source_traceability_rate": expected_rate(
                self.traceable_recommendation_count,
                self.grounded_recommendation_count,
            ),
            "labelled_relevance_rate": expected_rate(
                self.allowed_recommendation_count,
                self.recommendation_count,
            ),
            "recommendation_group_coverage_rate": expected_rate(
                self.matched_recommendation_group_count,
                self.required_recommendation_group_count,
            ),
        }
        for field_name, expected_rate_value in rate_aggregates.items():
            if getattr(self, field_name) != expected_rate_value:
                raise ValueError(f"{field_name} must match Stay aggregate counts")
        truth_boundary = (
            self.unverified_price_field_count == 0
            and self.unknown_availability_count == self.recommendation_count
            and self.booking_disabled_count == self.recommendation_count
        )
        if self.commercial_truth_boundary_passed != truth_boundary:
            raise ValueError("commercial truth boundary must match Stay result counts")
        latencies = sorted(
            item.query_latency_ms + item.selection_latency_ms
            for item in self.results
            if item.model_call_count > 0
        )
        if self.p50_case_latency_ms != nearest_rank(latencies, 50):
            raise ValueError("p50_case_latency_ms must match Stay case results")
        if self.p95_case_latency_ms != nearest_rank(latencies, 95):
            raise ValueError("p95_case_latency_ms must match Stay case results")
        return self


def nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    rank = (percentile * len(values) + 99) // 100
    return values[max(rank - 1, 0)]
