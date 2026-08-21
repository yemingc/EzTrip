from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.agents.contracts import ExploreQueryKind, ModelTokenUsage
from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.candidates import ActivityEnvironment, GeoPoint
from app.domain.request import TripRequest
from app.evaluation.contracts import EvaluationCheck, expected_rate


class ExploreFixtureCandidateSpec(DomainModel):
    candidate_id: Identifier
    provider_id: NonEmptyText
    name: NonEmptyText
    city: NonEmptyText
    district: NonEmptyText | None = None
    address: NonEmptyText | None = None
    location: GeoPoint
    categories: tuple[NonEmptyText, ...] = Field(min_length=1)
    environment: ActivityEnvironment
    tags: tuple[NonEmptyText, ...] = ()
    data_mode: Literal["fixture"] = "fixture"


class ExploreAgentExpectation(DomainModel):
    required_query_kinds: tuple[ExploreQueryKind, ...] = Field(min_length=1, max_length=2)
    required_context_refs: tuple[NonEmptyText, ...] = ()
    allowed_recommendation_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=3)
    forbidden_recommendation_ids: tuple[Identifier, ...] = Field(max_length=2)
    required_recommendation_groups: tuple[tuple[Identifier, ...], ...] = Field(
        min_length=1,
        max_length=2,
    )

    @model_validator(mode="after")
    def validate_unique_expectations(self) -> "ExploreAgentExpectation":
        if len(self.required_query_kinds) != len(set(self.required_query_kinds)):
            raise ValueError("required Explore query kinds must be unique")
        allowed = set(self.allowed_recommendation_ids)
        forbidden = set(self.forbidden_recommendation_ids)
        if allowed & forbidden:
            raise ValueError("allowed and forbidden Explore candidates cannot overlap")
        if any(
            not group or not set(group).issubset(allowed)
            for group in self.required_recommendation_groups
        ):
            raise ValueError(
                "required recommendation groups must be non-empty subsets of allowed ids"
            )
        return self


class ExploreAgentEvalCase(DomainModel):
    case_id: Identifier
    title: NonEmptyText
    request: TripRequest
    provider_candidates: tuple[ExploreFixtureCandidateSpec, ...] = Field(
        min_length=3,
        max_length=3,
    )
    expected: ExploreAgentExpectation
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_inventory(self) -> "ExploreAgentEvalCase":
        if not self.case_id.startswith("explore-") or not self.case_id.endswith("-v1"):
            raise ValueError("Explore case_id must encode the suite and version")
        candidate_ids = [item.candidate_id for item in self.provider_candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Explore provider candidate ids must be unique")
        candidate_set = set(candidate_ids)
        labelled = set(self.expected.allowed_recommendation_ids) | set(
            self.expected.forbidden_recommendation_ids
        )
        if labelled != candidate_set:
            raise ValueError(
                "every Explore fixture candidate must be labelled allowed or forbidden"
            )
        if any(item.city != self.request.destination_city for item in self.provider_candidates):
            raise ValueError("Explore fixture candidate cities must match the request")
        return self


class ExploreAgentEvalSuite(DomainModel):
    suite: Literal["explore-agent-v1"] = "explore-agent-v1"
    version: Literal[1] = 1
    data_mode: Literal["fixture"] = "fixture"
    cases: tuple[ExploreAgentEvalCase, ...] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def validate_suite_inventory(self) -> "ExploreAgentEvalSuite":
        case_ids = [item.case_id for item in self.cases]
        request_ids = [item.request.request_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Explore evaluation case ids must be unique")
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("Explore evaluation request ids must be unique")
        return self


class ExploreAgentCaseResult(DomainModel):
    case_id: Identifier
    passed: bool
    model_call_count: int = Field(ge=0, le=2)
    provider_call_count: int = Field(ge=0, le=4)
    query_count: int = Field(ge=0, le=4)
    required_query_kind_count: int = Field(ge=1, le=2)
    matched_query_kind_count: int = Field(ge=0, le=2)
    recommendation_count: int = Field(ge=0, le=6)
    grounded_recommendation_count: int = Field(ge=0, le=6)
    traceable_recommendation_count: int = Field(ge=0, le=6)
    allowed_recommendation_count: int = Field(ge=0, le=6)
    required_recommendation_group_count: int = Field(ge=1, le=2)
    matched_recommendation_group_count: int = Field(ge=0, le=2)
    query_latency_ms: int = Field(ge=0)
    selection_latency_ms: int = Field(ge=0)
    query_usage: ModelTokenUsage | None = None
    selection_usage: ModelTokenUsage | None = None
    error_code: Identifier | None = None
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_result(self) -> "ExploreAgentCaseResult":
        if self.matched_query_kind_count > self.required_query_kind_count:
            raise ValueError("matched query kinds cannot exceed required query kinds")
        if self.grounded_recommendation_count > self.recommendation_count:
            raise ValueError("grounded recommendations cannot exceed recommendations")
        if self.traceable_recommendation_count > self.grounded_recommendation_count:
            raise ValueError("traceable recommendations cannot exceed grounded recommendations")
        if self.allowed_recommendation_count > self.recommendation_count:
            raise ValueError("allowed recommendations cannot exceed recommendations")
        if self.matched_recommendation_group_count > self.required_recommendation_group_count:
            raise ValueError("matched recommendation groups cannot exceed required groups")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("Explore case passed must equal the conjunction of checks")
        return self


class ExploreAgentBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["explore-agent-v1"] = "explore-agent-v1"
    agent_version: Literal["explore-agent-v1"] = "explore-agent-v1"
    query_prompt_version: Literal["explore-query-strategy-v1"] = "explore-query-strategy-v1"
    selection_prompt_version: Literal["explore-candidate-selection-v1"] = (
        "explore-candidate-selection-v1"
    )
    execution_mode: Literal["fixture", "live"]
    model: NonEmptyText
    dataset_sha256: Sha256Digest
    case_count: Literal[6] = 6
    passed_case_count: int = Field(ge=0, le=6)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    model_call_count: int = Field(ge=0, le=12)
    provider_call_count: int = Field(ge=0, le=24)
    required_query_kind_count: int = Field(ge=6, le=12)
    matched_query_kind_count: int = Field(ge=0, le=12)
    query_kind_coverage_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    recommendation_count: int = Field(ge=0)
    grounded_recommendation_count: int = Field(ge=0)
    grounding_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    traceable_recommendation_count: int = Field(ge=0)
    source_traceability_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    allowed_recommendation_count: int = Field(ge=0)
    labelled_relevance_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    required_recommendation_group_count: int = Field(ge=6, le=12)
    matched_recommendation_group_count: int = Field(ge=0, le=12)
    recommendation_group_coverage_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    usage_call_count: int = Field(ge=0, le=12)
    total_prompt_tokens: int = Field(ge=0)
    total_completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    p50_case_latency_ms: int = Field(ge=0)
    p95_case_latency_ms: int = Field(ge=0)
    results: tuple[ExploreAgentCaseResult, ...] = Field(min_length=6, max_length=6)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report_aggregates(self) -> "ExploreAgentBaselineReport":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("Explore report case ids must be unique")
        scalar_aggregates = {
            "passed_case_count": sum(item.passed for item in self.results),
            "model_call_count": sum(item.model_call_count for item in self.results),
            "provider_call_count": sum(item.provider_call_count for item in self.results),
            "required_query_kind_count": sum(
                item.required_query_kind_count for item in self.results
            ),
            "matched_query_kind_count": sum(item.matched_query_kind_count for item in self.results),
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
                raise ValueError(f"{field_name} must match Explore case results")
        rate_aggregates = {
            "case_pass_rate": expected_rate(self.passed_case_count, self.case_count),
            "query_kind_coverage_rate": expected_rate(
                self.matched_query_kind_count,
                self.required_query_kind_count,
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
                raise ValueError(f"{field_name} must match Explore aggregate counts")
        latencies = sorted(
            item.query_latency_ms + item.selection_latency_ms
            for item in self.results
            if item.model_call_count > 0
        )
        if self.p50_case_latency_ms != _nearest_rank(latencies, 50):
            raise ValueError("p50_case_latency_ms must match Explore case results")
        if self.p95_case_latency_ms != _nearest_rank(latencies, 95):
            raise ValueError("p95_case_latency_ms must match Explore case results")
        return self


def _nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    rank = (percentile * len(values) + 99) // 100
    return values[max(rank - 1, 0)]
