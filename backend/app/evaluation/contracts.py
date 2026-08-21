from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.agents.contracts import ModelTokenUsage
from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerCapability, PlannerReadiness
from app.domain.provider import ProviderFailure
from app.domain.request import (
    ConstraintKind,
    ConstraintSource,
    ConstraintStrength,
    TripRequest,
)
from app.domain.sources import DataMode
from app.domain.workflow import PlanningWorkflowStatus

RATE_QUANTUM = Decimal("0.0001")


def expected_rate(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal("1.0000")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        RATE_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


class SeedTier(StrEnum):
    STANDARD = "standard"
    HARD = "hard"


class SeedProviderBehavior(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    FORBIDDEN = "forbidden"


class ExpectedPOISearchCall(DomainModel):
    keywords: NonEmptyText
    city_adcode: str = Field(pattern=r"^\d{6}$")
    limit: int = Field(default=1, ge=1, le=3)


class PlanningSeedProviderSpec(DomainModel):
    behavior: SeedProviderBehavior
    expected_calls: tuple[ExpectedPOISearchCall, ...] = ()
    candidates: tuple[CandidatePOI, ...] = ()
    failure: ProviderFailure | None = None

    @model_validator(mode="after")
    def validate_behavior_payload(self) -> "PlanningSeedProviderSpec":
        if any(candidate.source.data_mode != DataMode.FIXTURE for candidate in self.candidates):
            raise ValueError("seed candidates must be explicitly labelled as fixture data")
        if self.behavior == SeedProviderBehavior.SUCCESS:
            if not self.expected_calls or not self.candidates or self.failure is not None:
                raise ValueError("success provider requires calls and candidates without failure")
        elif self.behavior == SeedProviderBehavior.FAILURE:
            if not self.expected_calls or self.candidates or self.failure is None:
                raise ValueError(
                    "failure provider requires calls and one failure without candidates"
                )
        elif self.expected_calls or self.candidates or self.failure is not None:
            raise ValueError("forbidden provider cannot define calls, candidates, or failure")
        return self


class ExpectedConstraintBuckets(DomainModel):
    confirmed_hard: tuple[Identifier, ...] = ()
    confirmed_soft: tuple[Identifier, ...] = ()
    pending: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_unique_constraint_ids(self) -> "ExpectedConstraintBuckets":
        all_ids = (*self.confirmed_hard, *self.confirmed_soft, *self.pending)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("expected constraint ids must appear in exactly one bucket")
        return self


class PlanningSeedExpectation(DomainModel):
    status: PlanningWorkflowStatus
    readiness: PlannerReadiness
    ready_capabilities: tuple[PlannerCapability, ...]
    blocked_capabilities: tuple[PlannerCapability, ...]
    candidate_count: int = Field(ge=0)
    candidate_names: tuple[NonEmptyText, ...] = ()
    constraint_buckets: ExpectedConstraintBuckets

    @model_validator(mode="after")
    def validate_expected_partitions(self) -> "PlanningSeedExpectation":
        ready = set(self.ready_capabilities)
        blocked = set(self.blocked_capabilities)
        if ready & blocked or ready | blocked != set(PlannerCapability):
            raise ValueError("expected capabilities must form a complete non-overlapping partition")
        if len(self.ready_capabilities) != len(ready) or len(self.blocked_capabilities) != len(
            blocked
        ):
            raise ValueError("expected capability lists cannot contain duplicates")
        if self.candidate_count != len(self.candidate_names):
            raise ValueError("candidate_count must match candidate_names")
        return self


class FutureExpectation(DomainModel):
    code: Identifier
    description: NonEmptyText


class PlanningSeedCase(DomainModel):
    version: Literal[1] = 1
    case_id: Identifier
    tier: SeedTier
    title: NonEmptyText
    request: TripRequest
    provider: PlanningSeedProviderSpec
    expected: PlanningSeedExpectation
    future_expectations: tuple[FutureExpectation, ...] = ()
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_semantics(self) -> "PlanningSeedCase":
        if not self.case_id.startswith(f"seed-{self.tier.value}-") or not self.case_id.endswith(
            "-v1"
        ):
            raise ValueError("case_id must encode the tier and version")

        actual_hard = tuple(
            item.constraint_id
            for item in self.request.constraints.items
            if item.confirmed and item.strength == ConstraintStrength.HARD
        )
        actual_soft = tuple(
            item.constraint_id
            for item in self.request.constraints.items
            if item.confirmed and item.strength == ConstraintStrength.SOFT
        )
        actual_pending = tuple(
            item.constraint_id for item in self.request.constraints.items if not item.confirmed
        )
        buckets = self.expected.constraint_buckets
        if (
            buckets.confirmed_hard != actual_hard
            or buckets.confirmed_soft != actual_soft
            or buckets.pending != actual_pending
        ):
            raise ValueError("expected constraint buckets must match the TripRequest")

        if self.provider.behavior == SeedProviderBehavior.SUCCESS:
            if self.expected.status != PlanningWorkflowStatus.CANDIDATES_READY:
                raise ValueError("success provider cases must expect candidates_ready")
            candidate_names = tuple(candidate.name for candidate in self.provider.candidates)
            if self.expected.candidate_names != candidate_names:
                raise ValueError("expected candidate names must match provider candidates")
        elif self.provider.behavior == SeedProviderBehavior.FAILURE:
            if self.expected.status != PlanningWorkflowStatus.PROVIDER_FAILED:
                raise ValueError("failure provider cases must expect provider_failed")
            if self.expected.candidate_count != 0:
                raise ValueError("failure seed cases cannot expect fabricated candidates")
        elif self.expected.status not in {
            PlanningWorkflowStatus.NEEDS_CLARIFICATION,
            PlanningWorkflowStatus.NO_CANDIDATE_QUERY,
        }:
            raise ValueError("forbidden provider cases must stop or skip before provider access")
        return self


class PlanningSeedManifestEntry(DomainModel):
    case_id: Identifier
    tier: SeedTier
    path: NonEmptyText


class PlanningSeedManifest(DomainModel):
    suite: Literal["planning-seed-v1"] = "planning-seed-v1"
    version: Literal[1] = 1
    status: Literal["executable_baseline"] = "executable_baseline"
    case_schema: Literal["../../schemas/planning-seed-case.v1.json"] = (
        "../../schemas/planning-seed-case.v1.json"
    )
    cases: tuple[PlanningSeedManifestEntry, ...] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_case_inventory(self) -> "PlanningSeedManifest":
        ids = [item.case_id for item in self.cases]
        paths = [item.path for item in self.cases]
        if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise ValueError("manifest case ids and paths must be unique")
        if sum(item.tier == SeedTier.STANDARD for item in self.cases) != 6:
            raise ValueError("manifest must contain six standard cases")
        if sum(item.tier == SeedTier.HARD for item in self.cases) != 4:
            raise ValueError("manifest must contain four hard cases")
        return self


class EvaluationCheck(DomainModel):
    code: Identifier
    passed: bool


class PlanningSeedCaseResult(DomainModel):
    case_id: Identifier
    tier: SeedTier
    passed: bool
    expected_status: PlanningWorkflowStatus
    actual_status: PlanningWorkflowStatus
    provider_call_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    traceable_candidate_count: int = Field(ge=0)
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_result(self) -> "PlanningSeedCaseResult":
        if self.traceable_candidate_count > self.candidate_count:
            raise ValueError("traceable candidates cannot exceed total candidates")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("case passed must equal the conjunction of checks")
        return self


class PlanningSeedBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["planning-seed-v1"] = "planning-seed-v1"
    workflow_version: Literal["minimal-planning-graph-v1"] = "minimal-planning-graph-v1"
    dataset_sha256: Sha256Digest
    case_count: Literal[10] = 10
    standard_case_count: Literal[6] = 6
    hard_case_count: Literal[4] = 4
    passed_case_count: int = Field(ge=0, le=10)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    check_count: int = Field(ge=1)
    passed_check_count: int = Field(ge=0)
    check_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    candidate_count: int = Field(ge=0)
    traceable_candidate_count: int = Field(ge=0)
    source_traceability_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    results: tuple[PlanningSeedCaseResult, ...] = Field(min_length=10, max_length=10)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report_aggregates(self) -> "PlanningSeedBaselineReport":
        result_ids = [item.case_id for item in self.results]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("baseline result case ids must be unique")
        if sum(item.tier == SeedTier.STANDARD for item in self.results) != 6:
            raise ValueError("baseline report must contain six standard results")
        if sum(item.tier == SeedTier.HARD for item in self.results) != 4:
            raise ValueError("baseline report must contain four hard results")
        if self.passed_case_count != sum(item.passed for item in self.results):
            raise ValueError("passed_case_count must match case results")
        checks = [check for item in self.results for check in item.checks]
        if self.check_count != len(checks):
            raise ValueError("check_count must match case result checks")
        if self.passed_check_count != sum(check.passed for check in checks):
            raise ValueError("passed_check_count must match check results")
        if self.candidate_count != sum(item.candidate_count for item in self.results):
            raise ValueError("candidate_count must match case results")
        if self.traceable_candidate_count != sum(
            item.traceable_candidate_count for item in self.results
        ):
            raise ValueError("traceable_candidate_count must match case results")
        expected_case_rate = expected_rate(self.passed_case_count, self.case_count)
        expected_check_rate = expected_rate(self.passed_check_count, self.check_count)
        expected_source_rate = expected_rate(
            self.traceable_candidate_count,
            self.candidate_count,
        )
        if self.case_pass_rate != expected_case_rate:
            raise ValueError("case_pass_rate must match aggregate counts")
        if self.check_pass_rate != expected_check_rate:
            raise ValueError("check_pass_rate must match aggregate counts")
        if self.source_traceability_rate != expected_source_rate:
            raise ValueError("source_traceability_rate must match aggregate counts")
        return self


class ConstraintEvaluationLabel(DomainModel):
    kind: ConstraintKind
    value: NonEmptyText
    strength: ConstraintStrength
    source: ConstraintSource
    confirmed: bool

    @model_validator(mode="after")
    def validate_confirmation_source(self) -> "ConstraintEvaluationLabel":
        if self.kind == ConstraintKind.WALKING_INTENSITY and self.value not in {
            "low",
            "medium",
            "high",
        }:
            raise ValueError("walking intensity evaluation labels must be canonical")
        if self.source == ConstraintSource.USER_EXPLICIT and not self.confirmed:
            raise ValueError("user_explicit evaluation labels must be confirmed")
        if self.source == ConstraintSource.AGENT_INFERRED and self.confirmed:
            raise ValueError("agent_inferred evaluation labels must remain unconfirmed")
        if self.source not in {
            ConstraintSource.USER_EXPLICIT,
            ConstraintSource.AGENT_INFERRED,
        }:
            raise ValueError("Constraint Agent V1 labels only support extraction sources")
        return self


class ConstraintAgentExpectationCase(DomainModel):
    case_id: Identifier
    expected_constraints: tuple[ConstraintEvaluationLabel, ...] = Field(max_length=12)

    @model_validator(mode="after")
    def validate_unique_semantics(self) -> "ConstraintAgentExpectationCase":
        keys = [(item.kind, item.value.casefold()) for item in self.expected_constraints]
        if len(keys) != len(set(keys)):
            raise ValueError("expected Agent constraints must be semantically unique")
        return self


class ConstraintAgentExpectationSuite(DomainModel):
    suite: Literal["constraint-agent-expectations-v1"] = "constraint-agent-expectations-v1"
    version: Literal[1] = 1
    source_planning_seed_sha256: Sha256Digest
    cases: tuple[ConstraintAgentExpectationCase, ...] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_case_inventory(self) -> "ConstraintAgentExpectationSuite":
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise ValueError("constraint Agent expectation case ids must be unique")
        return self


class ConstraintAgentCaseResult(DomainModel):
    case_id: Identifier
    tier: SeedTier
    passed: bool
    expected_constraints: tuple[ConstraintEvaluationLabel, ...]
    actual_constraints: tuple[ConstraintEvaluationLabel, ...]
    expected_constraint_count: int = Field(ge=0)
    actual_constraint_count: int = Field(ge=0)
    semantic_match_count: int = Field(ge=0)
    confirmation_match_count: int = Field(ge=0)
    clarification_match: bool
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None
    error_code: Identifier | None = None
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_constraint_agent_case_result(self) -> "ConstraintAgentCaseResult":
        if self.expected_constraint_count != len(self.expected_constraints):
            raise ValueError("expected_constraint_count must match labels")
        if self.actual_constraint_count != len(self.actual_constraints):
            raise ValueError("actual_constraint_count must match labels")
        if self.semantic_match_count > min(
            self.expected_constraint_count,
            self.actual_constraint_count,
        ):
            raise ValueError("semantic matches cannot exceed expected or actual constraints")
        if self.confirmation_match_count > self.semantic_match_count:
            raise ValueError("confirmation matches cannot exceed semantic matches")
        expected_semantic = {
            (item.kind, item.value.casefold(), item.strength) for item in self.expected_constraints
        }
        actual_semantic = {
            (item.kind, item.value.casefold(), item.strength) for item in self.actual_constraints
        }
        if self.semantic_match_count != len(expected_semantic & actual_semantic):
            raise ValueError("semantic_match_count must be recomputed from labels")
        expected_confirmation = {
            (item.kind, item.value.casefold(), item.strength, item.source, item.confirmed)
            for item in self.expected_constraints
        }
        actual_confirmation = {
            (item.kind, item.value.casefold(), item.strength, item.source, item.confirmed)
            for item in self.actual_constraints
        }
        if self.confirmation_match_count != len(expected_confirmation & actual_confirmation):
            raise ValueError("confirmation_match_count must be recomputed from labels")
        expected_pending = {
            (item.kind, item.value.casefold(), item.strength)
            for item in self.expected_constraints
            if not item.confirmed
        }
        actual_pending = {
            (item.kind, item.value.casefold(), item.strength)
            for item in self.actual_constraints
            if not item.confirmed
        }
        if self.clarification_match != (expected_pending == actual_pending):
            raise ValueError("clarification_match must be recomputed from labels")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("case passed must equal the conjunction of checks")
        if (self.error_code is None) != self.checks[0].passed:
            raise ValueError("the first check must represent protocol success")
        return self


class ConstraintAgentBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["constraint-agent-planning-seed-v1"] = "constraint-agent-planning-seed-v1"
    agent_version: Literal["constraint-agent-v1"] = "constraint-agent-v1"
    prompt_version: Literal["constraint-extraction-v1"] = "constraint-extraction-v1"
    execution_mode: Literal["fixture", "live"]
    model: NonEmptyText
    dataset_sha256: Sha256Digest
    case_count: Literal[10] = 10
    passed_case_count: int = Field(ge=0, le=10)
    exact_case_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    expected_constraint_count: int = Field(ge=0)
    actual_constraint_count: int = Field(ge=0)
    semantic_match_count: int = Field(ge=0)
    semantic_precision: Decimal = Field(ge=0, le=1, decimal_places=4)
    semantic_recall: Decimal = Field(ge=0, le=1, decimal_places=4)
    confirmation_match_count: int = Field(ge=0)
    confirmation_accuracy: Decimal = Field(ge=0, le=1, decimal_places=4)
    clarification_match_case_count: int = Field(ge=0, le=10)
    clarification_case_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    usage_case_count: int = Field(ge=0, le=10)
    total_prompt_tokens: int = Field(ge=0)
    total_completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    p50_latency_ms: int = Field(ge=0)
    p95_latency_ms: int = Field(ge=0)
    results: tuple[ConstraintAgentCaseResult, ...] = Field(min_length=10, max_length=10)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_constraint_agent_aggregates(self) -> "ConstraintAgentBaselineReport":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("constraint Agent report case ids must be unique")
        aggregates = {
            "passed_case_count": sum(item.passed for item in self.results),
            "expected_constraint_count": sum(
                item.expected_constraint_count for item in self.results
            ),
            "actual_constraint_count": sum(item.actual_constraint_count for item in self.results),
            "semantic_match_count": sum(item.semantic_match_count for item in self.results),
            "confirmation_match_count": sum(item.confirmation_match_count for item in self.results),
            "clarification_match_case_count": sum(
                item.clarification_match for item in self.results
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
        for aggregate_field_name, aggregate_expected in aggregates.items():
            if getattr(self, aggregate_field_name) != aggregate_expected:
                raise ValueError(f"{aggregate_field_name} must match case results")

        rates = {
            "exact_case_rate": expected_rate(self.passed_case_count, self.case_count),
            "semantic_precision": expected_rate(
                self.semantic_match_count,
                self.actual_constraint_count,
            ),
            "semantic_recall": expected_rate(
                self.semantic_match_count,
                self.expected_constraint_count,
            ),
            "confirmation_accuracy": expected_rate(
                self.confirmation_match_count,
                self.semantic_match_count,
            ),
            "clarification_case_rate": expected_rate(
                self.clarification_match_case_count,
                self.case_count,
            ),
        }
        for rate_field_name, rate_expected in rates.items():
            if getattr(self, rate_field_name) != rate_expected:
                raise ValueError(f"{rate_field_name} must match aggregate counts")

        latencies = sorted(item.latency_ms for item in self.results)
        if self.p50_latency_ms != _nearest_rank(latencies, 50):
            raise ValueError("p50_latency_ms must match case results")
        if self.p95_latency_ms != _nearest_rank(latencies, 95):
            raise ValueError("p95_latency_ms must match case results")
        return self


def _nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    rank = (percentile * len(values) + 99) // 100
    return values[max(rank - 1, 0)]


class SinglePlannerOutcome(StrEnum):
    PLANNED = "planned"
    SKIPPED = "skipped"
    FAILED = "failed"


class SinglePlannerCaseResult(DomainModel):
    case_id: Identifier
    tier: SeedTier
    passed: bool
    upstream_status: PlanningWorkflowStatus
    outcome: SinglePlannerOutcome
    planning_expected: bool
    model_called: bool
    candidate_count: int = Field(ge=0)
    scheduled_candidate_count: int = Field(ge=0)
    grounded_item_count: int = Field(ge=0)
    traceable_item_count: int = Field(ge=0)
    valid_day_plan_count: int = Field(ge=0, le=5)
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None
    error_code: Identifier | None = None
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_single_planner_case(self) -> "SinglePlannerCaseResult":
        if self.scheduled_candidate_count > self.candidate_count:
            raise ValueError("scheduled candidates cannot exceed input candidates")
        if self.grounded_item_count > self.scheduled_candidate_count:
            raise ValueError("grounded items cannot exceed scheduled candidates")
        if self.traceable_item_count > self.grounded_item_count:
            raise ValueError("traceable items cannot exceed grounded items")
        if self.planning_expected != (
            self.upstream_status == PlanningWorkflowStatus.CANDIDATES_READY
        ):
            raise ValueError("planning_expected must follow the upstream workflow status")
        if self.outcome == SinglePlannerOutcome.PLANNED:
            if not self.planning_expected or not self.model_called or self.error_code is not None:
                raise ValueError("planned cases require an eligible model call without error")
        elif self.outcome == SinglePlannerOutcome.SKIPPED:
            if self.planning_expected or self.model_called or self.error_code is not None:
                raise ValueError("skipped cases must stop before the model")
            if any(
                (
                    self.scheduled_candidate_count,
                    self.grounded_item_count,
                    self.traceable_item_count,
                    self.valid_day_plan_count,
                    self.latency_ms,
                )
            ):
                raise ValueError("skipped cases cannot contain Planner outputs")
        elif not self.planning_expected or not self.model_called or self.error_code is None:
            raise ValueError("failed cases require an eligible model call and error code")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("case passed must equal the conjunction of checks")
        return self


class SinglePlannerBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["single-planner-planning-seed-v1"] = "single-planner-planning-seed-v1"
    workflow_version: Literal["minimal-planning-graph-v1"] = "minimal-planning-graph-v1"
    agent_version: Literal["single-planner-v1"] = "single-planner-v1"
    prompt_version: Literal["candidate-placement-v1"] = "candidate-placement-v1"
    execution_mode: Literal["fixture", "live"]
    model: NonEmptyText
    dataset_sha256: Sha256Digest
    case_count: Literal[10] = 10
    planning_expected_case_count: int = Field(ge=0, le=10)
    model_call_count: int = Field(ge=0, le=10)
    planned_case_count: int = Field(ge=0, le=10)
    skipped_case_count: int = Field(ge=0, le=10)
    failed_case_count: int = Field(ge=0, le=10)
    passed_case_count: int = Field(ge=0, le=10)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    candidate_count: int = Field(ge=0)
    scheduled_candidate_count: int = Field(ge=0)
    candidate_coverage_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    grounded_item_count: int = Field(ge=0)
    grounding_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    traceable_item_count: int = Field(ge=0)
    source_traceability_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    usage_case_count: int = Field(ge=0, le=10)
    total_prompt_tokens: int = Field(ge=0)
    total_completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    p50_latency_ms: int = Field(ge=0)
    p95_latency_ms: int = Field(ge=0)
    results: tuple[SinglePlannerCaseResult, ...] = Field(min_length=10, max_length=10)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_single_planner_aggregates(self) -> "SinglePlannerBaselineReport":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("single Planner report case ids must be unique")
        aggregates = {
            "planning_expected_case_count": sum(item.planning_expected for item in self.results),
            "model_call_count": sum(item.model_called for item in self.results),
            "planned_case_count": sum(
                item.outcome == SinglePlannerOutcome.PLANNED for item in self.results
            ),
            "skipped_case_count": sum(
                item.outcome == SinglePlannerOutcome.SKIPPED for item in self.results
            ),
            "failed_case_count": sum(
                item.outcome == SinglePlannerOutcome.FAILED for item in self.results
            ),
            "passed_case_count": sum(item.passed for item in self.results),
            "candidate_count": sum(item.candidate_count for item in self.results),
            "scheduled_candidate_count": sum(
                item.scheduled_candidate_count for item in self.results
            ),
            "grounded_item_count": sum(item.grounded_item_count for item in self.results),
            "traceable_item_count": sum(item.traceable_item_count for item in self.results),
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
                raise ValueError(f"{field_name} must match case results")
        rates = {
            "case_pass_rate": expected_rate(self.passed_case_count, self.case_count),
            "candidate_coverage_rate": expected_rate(
                self.scheduled_candidate_count,
                self.candidate_count,
            ),
            "grounding_rate": expected_rate(
                self.grounded_item_count,
                self.scheduled_candidate_count,
            ),
            "source_traceability_rate": expected_rate(
                self.traceable_item_count,
                self.grounded_item_count,
            ),
        }
        for field_name, rate_expected in rates.items():
            if getattr(self, field_name) != rate_expected:
                raise ValueError(f"{field_name} must match aggregate counts")
        called_latencies = sorted(item.latency_ms for item in self.results if item.model_called)
        if self.p50_latency_ms != _nearest_rank(called_latencies, 50):
            raise ValueError("p50_latency_ms must match called cases")
        if self.p95_latency_ms != _nearest_rank(called_latencies, 95):
            raise ValueError("p95_latency_ms must match called cases")
        return self
