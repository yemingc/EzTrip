from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.candidates import CandidatePOI, CandidateStay
from app.domain.context import PlannerContext
from app.domain.planning import TripPlan
from app.domain.sources import DataMode
from app.domain.travel_data import WeatherRisk
from app.domain.validation import PlanValidationReport, RepairAction
from app.evaluation.comparison_contracts import (
    COMPARISON_ARMS,
    ComparisonArm,
    ComparisonOutcome,
    ComparisonScenario,
)
from app.evaluation.contracts import EvaluationCheck, SeedTier, expected_rate
from app.planning.material_contracts import BudgetAllocation, RouteMatrix
from app.planning.repair_contracts import (
    RepairOutcome,
    RepairRouterResult,
    RepairStopReason,
)


class ComparisonToolSnapshot(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    snapshot_version: Literal["comparison-tool-snapshot-v1"] = "comparison-tool-snapshot-v1"
    request_id: Identifier
    context_id: Identifier
    data_mode: Literal[DataMode.FIXTURE] = DataMode.FIXTURE
    planner_context: PlannerContext
    poi_candidates: tuple[CandidatePOI, ...] = Field(max_length=4)
    stay_candidates: tuple[CandidateStay, ...]
    route_anchor_candidate_id: Identifier | None
    weather_risks: tuple[WeatherRisk, ...]
    route_matrix: RouteMatrix
    budget_allocation: BudgetAllocation
    snapshot_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ComparisonToolSnapshot":
        identities = {
            (self.request_id, self.context_id),
            (self.planner_context.request_id, self.planner_context.context_id),
            (self.route_matrix.request_id, self.route_matrix.context_id),
            (self.budget_allocation.request_id, self.budget_allocation.context_id),
        }
        if len(identities) != 1:
            raise ValueError("comparison tool facts must preserve request identity")
        if self.route_matrix.data_mode != self.data_mode:
            raise ValueError("comparison route facts must preserve data mode")
        if self.route_matrix.poi_candidate_ids != tuple(
            item.candidate_id for item in self.poi_candidates
        ):
            raise ValueError("comparison route scope must match POI facts")
        stay_ids = tuple(item.candidate_id for item in self.stay_candidates)
        if len(stay_ids) != len(set(stay_ids)):
            raise ValueError("comparison stay facts must be unique")
        if self.route_anchor_candidate_id not in set(stay_ids) | {None}:
            raise ValueError("comparison route anchor must reference a stay fact")
        if self.route_matrix.primary_stay_id != self.route_anchor_candidate_id:
            raise ValueError("comparison route scope must match the stay fact")
        if any(
            item.city != self.planner_context.destination.normalized_name
            or item.source.data_mode != self.data_mode
            for item in self.weather_risks
        ):
            raise ValueError("comparison weather facts must match city and data mode")
        return self


class ComparisonRunOutput(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    runner_version: Literal["system-comparison-runner-v1"] = "system-comparison-runner-v1"
    evaluator_version: Literal["hard-trip-plan-validator-v1"] = "hard-trip-plan-validator-v1"
    case_id: Identifier
    arm: ComparisonArm
    outcome: ComparisonOutcome
    tool_snapshot_sha256: Sha256Digest
    fault_fixture_sha256: Sha256Digest
    selected_stay_candidate_id: Identifier | None
    plan: TripPlan | None
    initial_validation: PlanValidationReport | None
    final_validation: PlanValidationReport | None
    repair: RepairRouterResult | None = None
    model_call_count: int = Field(ge=0)
    provider_call_count: int = Field(ge=0)
    token_usage_complete: bool
    total_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_output(self) -> "ComparisonRunOutput":
        if self.token_usage_complete != (self.total_tokens is not None):
            raise ValueError("complete token usage requires an explicit total")
        if self.outcome == ComparisonOutcome.BLOCKED_BEFORE_PLAN:
            if any(
                (
                    self.selected_stay_candidate_id,
                    self.plan,
                    self.initial_validation,
                    self.final_validation,
                    self.repair,
                )
            ):
                raise ValueError("blocked comparison output cannot contain plan artifacts")
            return self
        if (
            self.selected_stay_candidate_id is None
            or self.plan is None
            or self.initial_validation is None
            or self.final_validation is None
        ):
            raise ValueError("non-blocked comparison output requires evaluated plan artifacts")
        if (
            self.plan.request_id != self.initial_validation.request_id
            or self.plan.request_id != self.final_validation.request_id
            or self.final_validation.plan_id != self.plan.plan_id
        ):
            raise ValueError("comparison output must preserve request and final plan identity")
        if self.repair is None:
            if self.initial_validation != self.final_validation:
                raise ValueError("non-repair arms must preserve their post-run evaluation")
            if self.outcome == ComparisonOutcome.REPAIRED:
                raise ValueError("repaired comparison output requires a Repair result")
        else:
            if (
                self.arm != ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR
                or self.initial_validation != self.repair.initial_report
                or self.final_validation != self.repair.final_report
                or self.plan != self.repair.final_plan
            ):
                raise ValueError("comparison repair output must preserve Router lineage")
            mapped_outcome = {
                RepairOutcome.ALREADY_FINALIZABLE: (ComparisonOutcome.FINALIZABLE_WITHOUT_REPAIR),
                RepairOutcome.REPAIRED: ComparisonOutcome.REPAIRED,
                RepairOutcome.WAITING_FOR_USER: ComparisonOutcome.WAITING_FOR_USER,
                RepairOutcome.UNRESOLVED: ComparisonOutcome.UNRESOLVED,
            }[self.repair.outcome]
            if self.outcome != mapped_outcome:
                raise ValueError("comparison outcome must match Repair Router outcome")
        if self.outcome in {
            ComparisonOutcome.FINALIZABLE_WITHOUT_REPAIR,
            ComparisonOutcome.REPAIRED,
        }:
            if not self.final_validation.can_finalize:
                raise ValueError("successful comparison outcomes must be finalizable")
        elif self.final_validation.can_finalize:
            raise ValueError("waiting and unresolved comparison outcomes cannot finalize")
        return self


class ComparisonArmCaseResult(DomainModel):
    case_id: Identifier
    arm: ComparisonArm
    tier: SeedTier
    scenario: ComparisonScenario
    protocol_passed: bool
    frozen_expectation_match: bool | None
    outcome: ComparisonOutcome
    tool_snapshot_sha256: Sha256Digest
    fault_fixture_sha256: Sha256Digest
    selected_stay_candidate_id: Identifier | None
    plan_sha256: Sha256Digest | None
    initial_error_codes: tuple[NonEmptyText, ...]
    final_error_codes: tuple[NonEmptyText, ...]
    final_can_finalize: bool | None
    repair_actions: tuple[RepairAction, ...]
    repair_stop_reason: RepairStopReason | None
    scheduled_candidate_count: int = Field(ge=0)
    grounded_candidate_count: int = Field(ge=0)
    traceable_candidate_count: int = Field(ge=0)
    route_backed_candidate_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    provider_call_count: int = Field(ge=0)
    token_usage_complete: bool
    total_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> "ComparisonArmCaseResult":
        if self.protocol_passed != all(item.passed for item in self.checks):
            raise ValueError("comparison protocol pass must equal its check conjunction")
        if not (
            self.route_backed_candidate_count
            <= self.traceable_candidate_count
            <= self.grounded_candidate_count
            <= self.scheduled_candidate_count
        ):
            raise ValueError("comparison evidence counts must be monotonic")
        if self.token_usage_complete != (self.total_tokens is not None):
            raise ValueError("comparison token completeness must match total tokens")
        blocked = self.outcome == ComparisonOutcome.BLOCKED_BEFORE_PLAN
        if blocked != (self.plan_sha256 is None):
            raise ValueError("only blocked comparison results may omit a plan hash")
        if blocked:
            if any(
                (
                    self.initial_error_codes,
                    self.final_error_codes,
                    self.final_can_finalize is not None,
                    self.repair_actions,
                    self.repair_stop_reason is not None,
                    self.selected_stay_candidate_id is not None,
                    self.scheduled_candidate_count,
                )
            ):
                raise ValueError("blocked comparison result cannot contain plan-stage metrics")
        elif self.final_can_finalize is None or self.selected_stay_candidate_id is None:
            raise ValueError("evaluated comparison result requires finalization state")
        if self.arm == ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR:
            if self.frozen_expectation_match is None:
                raise ValueError("full Product Graph must check its frozen expectation")
        elif self.frozen_expectation_match is not None:
            raise ValueError("baseline arms do not own the full-graph expectation")
        return self


class ComparisonArmSummary(DomainModel):
    arm: ComparisonArm
    case_count: Literal[30] = 30
    protocol_passed_case_count: int = Field(ge=0, le=30)
    eligible_case_count: int = Field(ge=0, le=30)
    blocked_case_count: int = Field(ge=0, le=30)
    finalizable_case_count: int = Field(ge=0, le=30)
    finalization_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    finalizable_without_repair_case_count: int = Field(ge=0, le=30)
    repaired_case_count: int = Field(ge=0, le=30)
    waiting_for_user_case_count: int = Field(ge=0, le=30)
    unresolved_case_count: int = Field(ge=0, le=30)
    scheduled_candidate_count: int = Field(ge=0)
    grounding_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    source_traceability_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    route_lineage_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    model_call_count: int = Field(ge=0)
    provider_call_count: int = Field(ge=0)
    token_complete_case_count: int = Field(ge=0, le=30)
    total_tokens: int | None = Field(default=None, ge=0)
    p50_latency_ms: int | None = Field(default=None, ge=0)
    p95_latency_ms: int | None = Field(default=None, ge=0)
    results: tuple[ComparisonArmCaseResult, ...] = Field(min_length=30, max_length=30)

    @model_validator(mode="after")
    def validate_aggregates(self) -> "ComparisonArmSummary":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("comparison arm case ids must be unique")
        if any(item.arm != self.arm for item in self.results):
            raise ValueError("comparison arm summary cannot mix arms")
        outcome_counts = {
            outcome: sum(item.outcome == outcome for item in self.results)
            for outcome in ComparisonOutcome
        }
        aggregates = {
            "protocol_passed_case_count": sum(item.protocol_passed for item in self.results),
            "blocked_case_count": outcome_counts[ComparisonOutcome.BLOCKED_BEFORE_PLAN],
            "eligible_case_count": sum(
                item.outcome != ComparisonOutcome.BLOCKED_BEFORE_PLAN for item in self.results
            ),
            "finalizable_case_count": sum(item.final_can_finalize is True for item in self.results),
            "finalizable_without_repair_case_count": outcome_counts[
                ComparisonOutcome.FINALIZABLE_WITHOUT_REPAIR
            ],
            "repaired_case_count": outcome_counts[ComparisonOutcome.REPAIRED],
            "waiting_for_user_case_count": outcome_counts[ComparisonOutcome.WAITING_FOR_USER],
            "unresolved_case_count": outcome_counts[ComparisonOutcome.UNRESOLVED],
            "scheduled_candidate_count": sum(
                item.scheduled_candidate_count for item in self.results
            ),
            "model_call_count": sum(item.model_call_count for item in self.results),
            "provider_call_count": sum(item.provider_call_count for item in self.results),
            "token_complete_case_count": sum(item.token_usage_complete for item in self.results),
        }
        for field_name, expected in aggregates.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name} must match comparison arm results")
        if self.finalization_rate != expected_rate(
            self.finalizable_case_count, self.eligible_case_count
        ):
            raise ValueError("comparison finalization rate must match eligible results")
        evidence = {
            "grounding_rate": sum(item.grounded_candidate_count for item in self.results),
            "source_traceability_rate": sum(
                item.traceable_candidate_count for item in self.results
            ),
            "route_lineage_rate": sum(item.route_backed_candidate_count for item in self.results),
        }
        for field_name, numerator in evidence.items():
            if getattr(self, field_name) != expected_rate(
                numerator, self.scheduled_candidate_count
            ):
                raise ValueError(f"{field_name} must match comparison evidence counts")
        if self.token_complete_case_count == self.case_count:
            expected_tokens = sum(item.total_tokens or 0 for item in self.results)
            if self.total_tokens != expected_tokens:
                raise ValueError("complete comparison token totals must be aggregated")
        elif self.total_tokens is not None:
            raise ValueError("partial comparison token coverage cannot publish a total")
        latencies = sorted(item.latency_ms for item in self.results if item.latency_ms is not None)
        if len(latencies) != self.case_count:
            if self.p50_latency_ms is not None or self.p95_latency_ms is not None:
                raise ValueError("partial comparison latency cannot publish percentiles")
        else:
            p50 = _nearest_rank(latencies, 50)
            p95 = _nearest_rank(latencies, 95)
            if (self.p50_latency_ms, self.p95_latency_ms) != (p50, p95):
                raise ValueError("comparison latency percentiles must match case results")
        return self


class ComparisonPairedDelta(DomainModel):
    from_arm: ComparisonArm
    to_arm: ComparisonArm
    shared_eligible_case_count: int = Field(ge=0, le=30)
    improved_case_count: int = Field(ge=0, le=30)
    worsened_case_count: int = Field(ge=0, le=30)
    unchanged_case_count: int = Field(ge=0, le=30)
    finalization_rate_delta: Decimal = Field(ge=-1, le=1, decimal_places=4)

    @model_validator(mode="after")
    def validate_delta(self) -> "ComparisonPairedDelta":
        if self.from_arm == self.to_arm:
            raise ValueError("comparison delta requires two different arms")
        if (
            self.improved_case_count + self.worsened_case_count + self.unchanged_case_count
            != self.shared_eligible_case_count
        ):
            raise ValueError("comparison paired counts must cover shared eligible cases")
        return self


class SystemComparisonReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["system-comparison-v1"] = "system-comparison-v1"
    runner_version: Literal["system-comparison-runner-v1"] = "system-comparison-runner-v1"
    run_kind: Literal["fixture_control_path_replay"] = "fixture_control_path_replay"
    model: Literal["fixture-comparison-policy-v1"] = "fixture-comparison-policy-v1"
    evaluator_version: Literal["hard-trip-plan-validator-v1"] = "hard-trip-plan-validator-v1"
    dataset_sha256: Sha256Digest
    case_count: Literal[30] = 30
    live_calls_performed: Literal[False] = False
    control_path_claim_allowed: Literal[True] = True
    model_quality_claim_allowed: Literal[False] = False
    full_expectation_match_count: int = Field(ge=0, le=30)
    arms: tuple[ComparisonArmSummary, ...] = Field(min_length=3, max_length=3)
    paired_deltas: tuple[ComparisonPairedDelta, ...] = Field(min_length=3, max_length=3)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report(self) -> "SystemComparisonReport":
        if tuple(item.arm for item in self.arms) != COMPARISON_ARMS:
            raise ValueError("comparison report arms must preserve frozen order")
        expected_case_ids = tuple(item.case_id for item in self.arms[0].results)
        if any(
            tuple(item.case_id for item in arm.results) != expected_case_ids for arm in self.arms
        ):
            raise ValueError("comparison arms must preserve paired case order")
        for case_index in range(self.case_count):
            paired = tuple(arm.results[case_index] for arm in self.arms)
            if (
                len({item.tool_snapshot_sha256 for item in paired}) != 1
                or len({item.fault_fixture_sha256 for item in paired}) != 1
            ):
                raise ValueError("comparison paired cases must share input fixture hashes")
        full_results = self.arms[-1].results
        if self.full_expectation_match_count != sum(
            item.frozen_expectation_match is True for item in full_results
        ):
            raise ValueError("full expectation count must match full Product Graph results")
        expected_pairs = (
            (ComparisonArm.SINGLE_AGENT_TOOLS, ComparisonArm.PRODUCT_GRAPH_NO_HARD_GATE),
            (
                ComparisonArm.PRODUCT_GRAPH_NO_HARD_GATE,
                ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR,
            ),
            (ComparisonArm.SINGLE_AGENT_TOOLS, ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR),
        )
        if tuple((item.from_arm, item.to_arm) for item in self.paired_deltas) != expected_pairs:
            raise ValueError("comparison paired deltas must preserve frozen order")
        summaries = {item.arm: item for item in self.arms}
        for delta in self.paired_deltas:
            left = summaries[delta.from_arm]
            right = summaries[delta.to_arm]
            left_by_id = {item.case_id: item for item in left.results}
            right_by_id = {item.case_id: item for item in right.results}
            shared_ids = tuple(
                case_id
                for case_id in expected_case_ids
                if left_by_id[case_id].final_can_finalize is not None
                and right_by_id[case_id].final_can_finalize is not None
            )
            improved = sum(
                left_by_id[item].final_can_finalize is False
                and right_by_id[item].final_can_finalize is True
                for item in shared_ids
            )
            worsened = sum(
                left_by_id[item].final_can_finalize is True
                and right_by_id[item].final_can_finalize is False
                for item in shared_ids
            )
            unchanged = len(shared_ids) - improved - worsened
            expected_delta = (right.finalization_rate - left.finalization_rate).quantize(
                Decimal("0.0001")
            )
            if (
                delta.shared_eligible_case_count,
                delta.improved_case_count,
                delta.worsened_case_count,
                delta.unchanged_case_count,
                delta.finalization_rate_delta,
            ) != (len(shared_ids), improved, worsened, unchanged, expected_delta):
                raise ValueError("comparison paired delta must match arm results")
        return self


def _nearest_rank(values: list[int], percentile: int) -> int:
    rank = (percentile * len(values) + 99) // 100
    return values[max(rank - 1, 0)]


__all__ = [
    "ComparisonArmCaseResult",
    "ComparisonArmSummary",
    "ComparisonPairedDelta",
    "ComparisonRunOutput",
    "ComparisonToolSnapshot",
    "SystemComparisonReport",
]
