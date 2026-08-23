from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.agents.contracts import ModelTokenUsage
from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.validation import RepairAction
from app.evaluation.comparison_contracts import COMPARISON_ARMS, ComparisonArm
from app.evaluation.contracts import expected_rate
from app.planning.repair_contracts import RepairStopReason


class LiveCallOwner(StrEnum):
    SINGLE_AGENT = "single_agent"
    PRODUCT_SHARED_INITIAL = "product_shared_initial"
    PRODUCT_REPAIR = "product_repair"


class LiveCallPhase(StrEnum):
    BASE = "base"
    REPAIR = "repair"


class LiveModelNode(StrEnum):
    SINGLE_SELECTION = "single_selection"
    SINGLE_PLAN = "single_plan"
    EXPLORE_QUERY = "explore_query"
    EXPLORE_SELECTION = "explore_selection"
    STAY_QUERY = "stay_query"
    STAY_SELECTION = "stay_selection"
    PRODUCT_PLAN = "product_plan"


class LiveModelCallStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LiveModelCallRecord(DomainModel):
    call_index: int = Field(ge=1, le=54)
    trial_id: Identifier
    case_id: Identifier
    repetition: int = Field(ge=1, le=2)
    owner: LiveCallOwner
    phase: LiveCallPhase
    node: LiveModelNode
    model: NonEmptyText
    max_completion_tokens: int = Field(ge=1, le=1200)
    status: LiveModelCallStatus
    latency_ms: int | None = Field(default=None, ge=0)
    usage: ModelTokenUsage | None = None
    error_code: Identifier | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "LiveModelCallRecord":
        if self.phase == LiveCallPhase.REPAIR and self.owner != LiveCallOwner.PRODUCT_REPAIR:
            raise ValueError("repair calls must be owned by Product repair")
        if self.status == LiveModelCallStatus.STARTED:
            if self.latency_ms is not None or self.usage is not None or self.error_code is not None:
                raise ValueError("started calls cannot contain terminal fields")
        elif self.status == LiveModelCallStatus.SUCCEEDED:
            if self.latency_ms is None or self.error_code is not None:
                raise ValueError("successful calls require latency and no error code")
        elif self.latency_ms is None or self.error_code is None or self.usage is not None:
            raise ValueError("failed calls require latency/error and cannot claim usage")
        if self.usage is not None and self.usage.completion_tokens > self.max_completion_tokens:
            raise ValueError("actual completion tokens cannot exceed the reserved call ceiling")
        return self


class SingleSelectionProposal(DomainModel):
    poi_candidate_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=3)
    primary_stay_candidate_id: Identifier
    reason: NonEmptyText

    @model_validator(mode="after")
    def validate_selection(self) -> "SingleSelectionProposal":
        if len(self.poi_candidate_ids) != len(set(self.poi_candidate_ids)):
            raise ValueError("Single selection POI ids must be unique")
        return self


class SingleSelectionModelResponse(DomainModel):
    proposal: SingleSelectionProposal
    model: NonEmptyText
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None


class LiveTrialOutcome(StrEnum):
    FINALIZABLE_WITHOUT_REPAIR = "finalizable_without_repair"
    REPAIRED = "repaired"
    WAITING_FOR_USER = "waiting_for_user"
    UNRESOLVED = "unresolved"
    EXECUTION_FAILED = "execution_failed"


class LiveExecutionMode(StrEnum):
    FIXTURE_CONTRACT = "fixture_contract"
    LIVE = "live"


class LiveArmTrialResult(DomainModel):
    arm: ComparisonArm
    execution_succeeded: bool
    outcome: LiveTrialOutcome
    error_code: Identifier | None = None
    selected_poi_candidate_ids: tuple[Identifier, ...] = ()
    selected_stay_candidate_id: Identifier | None = None
    initial_plan_sha256: Sha256Digest | None = None
    final_plan_sha256: Sha256Digest | None = None
    initial_error_codes: tuple[NonEmptyText, ...] = ()
    final_error_codes: tuple[NonEmptyText, ...] = ()
    final_can_finalize: bool | None = None
    repair_actions: tuple[RepairAction, ...] = ()
    repair_stop_reason: RepairStopReason | None = None
    selected_poi_count: int = Field(ge=0, le=3)
    allowed_poi_count: int = Field(ge=0, le=3)
    required_poi_group_count: int = Field(ge=0, le=2)
    matched_poi_group_count: int = Field(ge=0, le=2)
    stay_selection_allowed: bool | None = None
    scheduled_candidate_count: int = Field(ge=0, le=3)
    grounded_candidate_count: int = Field(ge=0, le=3)
    traceable_candidate_count: int = Field(ge=0, le=3)
    route_backed_candidate_count: int = Field(ge=0, le=3)
    logical_model_call_indices: tuple[int, ...] = ()
    logical_model_call_count: int = Field(ge=0, le=7)
    token_usage_complete: bool
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    model_latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_result(self) -> "LiveArmTrialResult":
        if len(self.logical_model_call_indices) != len(set(self.logical_model_call_indices)):
            raise ValueError("logical call indices must be unique")
        if self.logical_model_call_count != len(self.logical_model_call_indices):
            raise ValueError("logical model-call count must match call indices")
        token_values = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        if self.token_usage_complete != all(item is not None for item in token_values):
            raise ValueError("token completeness must match all token totals")
        if self.token_usage_complete:
            assert self.prompt_tokens is not None
            assert self.completion_tokens is not None
            assert self.total_tokens is not None
            if self.total_tokens != self.prompt_tokens + self.completion_tokens:
                raise ValueError("arm token total must equal prompt plus completion")
        if not (
            self.route_backed_candidate_count
            <= self.traceable_candidate_count
            <= self.grounded_candidate_count
            <= self.scheduled_candidate_count
            <= self.selected_poi_count
        ):
            raise ValueError("live arm evidence counts must be monotonic")
        if self.selected_poi_count != len(self.selected_poi_candidate_ids):
            raise ValueError("selected POI count must match ids")
        if self.matched_poi_group_count > self.required_poi_group_count:
            raise ValueError("matched POI groups cannot exceed required groups")
        if self.execution_succeeded:
            if (
                self.error_code is not None
                or self.selected_stay_candidate_id is None
                or self.initial_plan_sha256 is None
                or self.final_plan_sha256 is None
                or self.final_can_finalize is None
                or self.stay_selection_allowed is None
                or self.outcome == LiveTrialOutcome.EXECUTION_FAILED
            ):
                raise ValueError("successful arm results require complete evaluated artifacts")
        elif (
            self.error_code is None
            or self.outcome != LiveTrialOutcome.EXECUTION_FAILED
            or self.final_can_finalize is not None
            or self.initial_plan_sha256 is not None
            or self.final_plan_sha256 is not None
        ):
            raise ValueError("failed arm results require only a stable execution error")
        if self.outcome in {
            LiveTrialOutcome.FINALIZABLE_WITHOUT_REPAIR,
            LiveTrialOutcome.REPAIRED,
        }:
            if self.final_can_finalize is not True:
                raise ValueError("successful outcomes must be finalizable")
        elif (
            self.outcome != LiveTrialOutcome.EXECUTION_FAILED
            and self.final_can_finalize is not False
        ):
            raise ValueError("waiting/unresolved outcomes cannot finalize")
        if self.arm != ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR and (
            self.repair_actions or self.repair_stop_reason is not None
        ):
            raise ValueError("only the full Product arm may contain repair evidence")
        return self


class LiveComparisonTrialResult(DomainModel):
    trial_id: Identifier
    case_id: Identifier
    repetition: int = Field(ge=1, le=2)
    trace_id: NonEmptyText | None = None
    input_catalog_sha256: Sha256Digest
    product_initial_plan_sha256: Sha256Digest | None = None
    product_initial_draft_shared: Literal[True] = True
    external_provider_call_count: Literal[0] = 0
    fixture_provider_read_count: int = Field(ge=0)
    physical_model_call_indices: tuple[int, ...]
    wall_clock_ms: int = Field(ge=0)
    arms: tuple[LiveArmTrialResult, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_trial(self) -> "LiveComparisonTrialResult":
        if tuple(item.arm for item in self.arms) != COMPARISON_ARMS:
            raise ValueError("live trial arms must preserve protocol order")
        if len(self.physical_model_call_indices) != len(set(self.physical_model_call_indices)):
            raise ValueError("physical trial call indices must be unique")
        single, no_gate, full = self.arms
        if no_gate.execution_succeeded != full.execution_succeeded:
            raise ValueError("Product arms must share initial execution success")
        if no_gate.execution_succeeded:
            if (
                self.product_initial_plan_sha256 is None
                or no_gate.initial_plan_sha256 != self.product_initial_plan_sha256
                or full.initial_plan_sha256 != self.product_initial_plan_sha256
            ):
                raise ValueError("Product arms must share the exact initial plan hash")
        elif self.product_initial_plan_sha256 is not None:
            raise ValueError("failed Product initial execution cannot publish a plan hash")
        if not set(single.logical_model_call_indices).issubset(self.physical_model_call_indices):
            raise ValueError("Single logical calls must belong to the trial")
        if not set(no_gate.logical_model_call_indices).issubset(self.physical_model_call_indices):
            raise ValueError("Product logical calls must belong to the trial")
        if not set(full.logical_model_call_indices).issubset(self.physical_model_call_indices):
            raise ValueError("full Product logical calls must belong to the trial")
        return self


class LiveComparisonArmSummary(DomainModel):
    arm: ComparisonArm
    trial_count: Literal[6] = 6
    execution_succeeded_trial_count: int = Field(ge=0, le=6)
    finalizable_trial_count: int = Field(ge=0, le=6)
    finalization_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    selected_poi_count: int = Field(ge=0)
    allowed_poi_count: int = Field(ge=0)
    labelled_relevance_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    required_poi_group_count: int = Field(ge=0)
    matched_poi_group_count: int = Field(ge=0)
    group_coverage_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    allowed_stay_trial_count: int = Field(ge=0, le=6)
    grounding_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    route_lineage_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    logical_model_call_count: int = Field(ge=0)
    exact_plan_consistency_case_count: int = Field(ge=0, le=3)
    comparable_consistency_case_count: int = Field(ge=0, le=3)
    p50_cumulative_model_latency_ms: int = Field(ge=0)
    p95_cumulative_model_latency_ms: int = Field(ge=0)


class LiveComparisonPairedDelta(DomainModel):
    from_arm: ComparisonArm
    to_arm: ComparisonArm
    shared_evaluable_trial_count: int = Field(ge=0, le=6)
    improved_trial_count: int = Field(ge=0, le=6)
    worsened_trial_count: int = Field(ge=0, le=6)
    unchanged_trial_count: int = Field(ge=0, le=6)
    finalization_rate_delta: Decimal = Field(ge=-1, le=1, decimal_places=4)

    @model_validator(mode="after")
    def validate_counts(self) -> "LiveComparisonPairedDelta":
        if self.from_arm == self.to_arm:
            raise ValueError("paired delta requires different arms")
        if (
            self.improved_trial_count + self.worsened_trial_count + self.unchanged_trial_count
            != self.shared_evaluable_trial_count
        ):
            raise ValueError("paired delta counts must cover shared evaluable trials")
        return self


class LiveComparisonPilotReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["live-system-comparison-pilot-v1"] = "live-system-comparison-pilot-v1"
    runner_version: Literal["live-system-comparison-runner-v1"] = "live-system-comparison-runner-v1"
    run_kind: Literal["repeated_development_pilot"] = "repeated_development_pilot"
    model: Literal["deepseek-v4-pro"] = "deepseek-v4-pro"
    temperature: Literal[0] = 0
    evaluator_version: Literal["hard-trip-plan-validator-v1"] = "hard-trip-plan-validator-v1"
    dataset_sha256: Sha256Digest
    started_at: datetime
    completed_at: datetime
    case_count: Literal[3] = 3
    repetitions_per_case: Literal[2] = 2
    trial_count: Literal[6] = 6
    execution_mode: LiveExecutionMode
    live_calls_performed: bool
    langsmith_tracing_enabled: bool
    external_provider_call_count: Literal[0] = 0
    amap_call_count: Literal[0] = 0
    max_model_calls: Literal[54] = 54
    physical_model_call_count: int = Field(ge=1, le=54)
    succeeded_model_call_count: int = Field(ge=0, le=54)
    failed_model_call_count: int = Field(ge=0, le=54)
    max_completion_tokens: Literal[55800] = 55800
    completion_token_reservation: int = Field(ge=1, le=55800)
    token_usage_complete: bool
    actual_prompt_tokens: int | None = Field(default=None, ge=0)
    actual_completion_tokens: int | None = Field(default=None, ge=0)
    actual_total_tokens: int | None = Field(default=None, ge=0)
    generalization_claim_allowed: Literal[False] = False
    model_quality_claim_scope: Literal["three_case_point_in_time_paired_observation"] = (
        "three_case_point_in_time_paired_observation"
    )
    calls: tuple[LiveModelCallRecord, ...] = Field(min_length=1, max_length=54)
    trials: tuple[LiveComparisonTrialResult, ...] = Field(min_length=6, max_length=6)
    arms: tuple[LiveComparisonArmSummary, ...] = Field(min_length=3, max_length=3)
    paired_deltas: tuple[LiveComparisonPairedDelta, ...] = Field(min_length=3, max_length=3)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report(self) -> "LiveComparisonPilotReport":
        if self.completed_at < self.started_at:
            raise ValueError("live report completion cannot precede start")
        expected_live = self.execution_mode == LiveExecutionMode.LIVE
        if self.live_calls_performed != expected_live:
            raise ValueError("live-call claim must match execution mode")
        if self.langsmith_tracing_enabled != expected_live:
            raise ValueError("LangSmith tracing claim must match execution mode")
        if tuple(item.call_index for item in self.calls) != tuple(range(1, len(self.calls) + 1)):
            raise ValueError("physical model call indexes must be contiguous")
        if any(item.status == LiveModelCallStatus.STARTED for item in self.calls):
            raise ValueError("completed reports cannot contain started calls")
        if self.physical_model_call_count != len(self.calls):
            raise ValueError("physical call count must match call records")
        if self.succeeded_model_call_count != sum(
            item.status == LiveModelCallStatus.SUCCEEDED for item in self.calls
        ):
            raise ValueError("successful call count must match records")
        if self.failed_model_call_count != sum(
            item.status == LiveModelCallStatus.FAILED for item in self.calls
        ):
            raise ValueError("failed call count must match records")
        if self.physical_model_call_count != (
            self.succeeded_model_call_count + self.failed_model_call_count
        ):
            raise ValueError("call status counts must cover physical calls")
        if self.completion_token_reservation != sum(
            item.max_completion_tokens for item in self.calls
        ):
            raise ValueError("completion reservation must match call records")
        complete_usage = all(item.usage is not None for item in self.calls)
        if self.token_usage_complete != complete_usage:
            raise ValueError("report token completeness must match physical calls")
        token_values = (
            self.actual_prompt_tokens,
            self.actual_completion_tokens,
            self.actual_total_tokens,
        )
        if self.token_usage_complete != all(item is not None for item in token_values):
            raise ValueError("complete report usage requires all token totals")
        if complete_usage:
            prompt = sum(item.usage.prompt_tokens for item in self.calls if item.usage is not None)
            completion = sum(
                item.usage.completion_tokens for item in self.calls if item.usage is not None
            )
            total = sum(item.usage.total_tokens for item in self.calls if item.usage is not None)
            reported = (
                self.actual_prompt_tokens,
                self.actual_completion_tokens,
                self.actual_total_tokens,
            )
            if reported != (prompt, completion, total):
                raise ValueError("actual token totals must match call usage")
        if tuple(item.arm for item in self.arms) != COMPARISON_ARMS:
            raise ValueError("live report arm summaries must preserve protocol order")
        trial_call_indices = tuple(
            index for trial in self.trials for index in trial.physical_model_call_indices
        )
        if sorted(trial_call_indices) != list(range(1, len(self.calls) + 1)):
            raise ValueError("trials must partition every physical model call")
        return self


class LiveRunJournalStatus(StrEnum):
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"


class LiveComparisonRunJournal(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["live-system-comparison-pilot-v1"] = "live-system-comparison-pilot-v1"
    runner_version: Literal["live-system-comparison-runner-v1"] = "live-system-comparison-runner-v1"
    status: LiveRunJournalStatus
    dataset_sha256: Sha256Digest
    model: NonEmptyText
    started_at: datetime
    updated_at: datetime
    current_trial_id: Identifier | None = None
    completed_trial_count: int = Field(ge=0, le=6)
    calls: tuple[LiveModelCallRecord, ...] = Field(max_length=54)
    trials: tuple[LiveComparisonTrialResult, ...] = Field(max_length=6)
    failure_code: Identifier | None = None

    @model_validator(mode="after")
    def validate_journal(self) -> "LiveComparisonRunJournal":
        if self.completed_trial_count != len(self.trials):
            raise ValueError("journal completed count must match trials")
        if (self.status == LiveRunJournalStatus.FAILED) != (self.failure_code is not None):
            raise ValueError("only failed journals carry a failure code")
        if self.status == LiveRunJournalStatus.COMPLETED and self.completed_trial_count != 6:
            raise ValueError("completed journals require all trials")
        return self


def summarize_rate(numerator: int, denominator: int) -> Decimal:
    return expected_rate(numerator, denominator)


__all__ = [
    "LiveArmTrialResult",
    "LiveCallOwner",
    "LiveCallPhase",
    "LiveComparisonArmSummary",
    "LiveComparisonPairedDelta",
    "LiveComparisonPilotReport",
    "LiveComparisonRunJournal",
    "LiveComparisonTrialResult",
    "LiveExecutionMode",
    "LiveModelCallRecord",
    "LiveModelCallStatus",
    "LiveModelNode",
    "LiveRunJournalStatus",
    "LiveTrialOutcome",
    "SingleSelectionModelResponse",
    "SingleSelectionProposal",
    "summarize_rate",
]
