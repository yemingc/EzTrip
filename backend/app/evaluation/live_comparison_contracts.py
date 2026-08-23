from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.evaluation.comparison_contracts import COMPARISON_ARMS, ComparisonArm, TravelerProfile


class LiveComparisonPilotProfile(StrEnum):
    BEIJING_HISTORY = "beijing_history"
    SHANGHAI_VIEW_FOOD = "shanghai_view_food"
    CHENGDU_FAMILY = "chengdu_family"


class LiveComparisonPilotCase(DomainModel):
    case_id: Identifier
    title: NonEmptyText
    source_plan_case_id: Identifier
    profile: LiveComparisonPilotProfile
    traveler_profile: TravelerProfile
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case(self) -> "LiveComparisonPilotCase":
        if not self.case_id.startswith("live-comparison-") or not self.case_id.endswith("-v1"):
            raise ValueError("live comparison case_id must encode suite and version")
        return self


class LiveComparisonFairnessContract(DomainModel):
    same_structured_request: Literal[True]
    same_frozen_provider_catalogs: Literal[True]
    same_model_name: Literal[True]
    temperature_zero: Literal[True]
    same_output_trip_plan_contract: Literal[True]
    same_post_run_evaluator: Literal[True]
    product_initial_draft_shared_between_product_arms: Literal[True]
    sequential_trial_execution: Literal[True]
    provider_refresh_during_run: Literal[False]
    amap_calls_allowed: Literal[False]
    langsmith_tracing_required: Literal[True]
    development_pilot_not_holdout: Literal[True]
    generalization_claim_allowed: Literal[False]


class LiveComparisonCallBudget(DomainModel):
    case_count: Literal[3] = 3
    repetitions_per_case: Literal[2] = 2
    trial_count: Literal[6] = 6
    single_selection_calls_per_trial: Literal[1] = 1
    single_plan_calls_per_trial: Literal[1] = 1
    product_explore_calls_per_trial: Literal[2] = 2
    product_stay_calls_per_trial: Literal[2] = 2
    product_plan_calls_per_trial: Literal[1] = 1
    base_model_calls_per_trial: Literal[7] = 7
    repair_model_call_allowance_per_trial: Literal[2] = 2
    max_model_calls: Literal[54] = 54
    single_selection_max_completion_tokens: Literal[900] = 900
    single_plan_max_completion_tokens: Literal[900] = 900
    product_explore_query_max_completion_tokens: Literal[900] = 900
    product_explore_selection_max_completion_tokens: Literal[1200] = 1200
    product_stay_query_max_completion_tokens: Literal[900] = 900
    product_stay_selection_max_completion_tokens: Literal[1200] = 1200
    product_plan_max_completion_tokens: Literal[900] = 900
    repair_call_max_completion_tokens: Literal[1200] = 1200
    base_completion_tokens_per_trial: Literal[6900] = 6900
    repair_completion_token_allowance_per_trial: Literal[2400] = 2400
    max_completion_tokens: Literal[55800] = 55800
    max_parallel_live_trials: Literal[1] = 1

    @model_validator(mode="after")
    def validate_budget(self) -> "LiveComparisonCallBudget":
        expected_trials = self.case_count * self.repetitions_per_case
        expected_base = (
            self.single_selection_calls_per_trial
            + self.single_plan_calls_per_trial
            + self.product_explore_calls_per_trial
            + self.product_stay_calls_per_trial
            + self.product_plan_calls_per_trial
        )
        expected_max = expected_trials * (
            expected_base + self.repair_model_call_allowance_per_trial
        )
        expected_base_completion = (
            self.single_selection_max_completion_tokens
            + self.single_plan_max_completion_tokens
            + self.product_explore_query_max_completion_tokens
            + self.product_explore_selection_max_completion_tokens
            + self.product_stay_query_max_completion_tokens
            + self.product_stay_selection_max_completion_tokens
            + self.product_plan_max_completion_tokens
        )
        expected_repair_completion = (
            self.repair_model_call_allowance_per_trial * self.repair_call_max_completion_tokens
        )
        expected_max_completion = expected_trials * (
            expected_base_completion + expected_repair_completion
        )
        if (
            self.trial_count,
            self.base_model_calls_per_trial,
            self.max_model_calls,
            self.base_completion_tokens_per_trial,
            self.repair_completion_token_allowance_per_trial,
            self.max_completion_tokens,
        ) != (
            expected_trials,
            expected_base,
            expected_max,
            expected_base_completion,
            expected_repair_completion,
            expected_max_completion,
        ):
            raise ValueError("live comparison call budget must match the frozen trial design")
        return self


class LiveComparisonPilotSuite(DomainModel):
    suite: Literal["live-system-comparison-pilot-v1"] = "live-system-comparison-pilot-v1"
    version: Literal[1] = 1
    dataset_role: Literal["repeated_development_pilot"] = "repeated_development_pilot"
    provider_mode: Literal["frozen_fixture_catalogs"] = "frozen_fixture_catalogs"
    model_provider: Literal["deepseek"] = "deepseek"
    model_name: Literal["deepseek-v4-pro"] = "deepseek-v4-pro"
    temperature: Literal[0] = 0
    repetitions_per_case: Literal[2] = 2
    arms: tuple[ComparisonArm, ...]
    fairness: LiveComparisonFairnessContract
    call_budget: LiveComparisonCallBudget
    cases: tuple[LiveComparisonPilotCase, ...] = Field(min_length=3, max_length=3)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_inventory(self) -> "LiveComparisonPilotSuite":
        if self.arms != COMPARISON_ARMS:
            raise ValueError("live comparison arms must preserve the system comparison order")
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise ValueError("live comparison case ids must be unique")
        if len({item.source_plan_case_id for item in self.cases}) != len(self.cases):
            raise ValueError("live comparison source Plan Agent cases must be unique")
        if {item.profile for item in self.cases} != set(LiveComparisonPilotProfile):
            raise ValueError("live comparison must cover the three frozen pilot profiles")
        if self.call_budget.case_count != len(self.cases):
            raise ValueError("live comparison call budget must match case inventory")
        if self.call_budget.repetitions_per_case != self.repetitions_per_case:
            raise ValueError("live comparison call budget must match repetitions")
        return self


class LiveComparisonPreflight(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["live-system-comparison-pilot-v1"] = "live-system-comparison-pilot-v1"
    dataset_sha256: Sha256Digest
    model: NonEmptyText
    expected_model: Literal["deepseek-v4-pro"] = "deepseek-v4-pro"
    model_matches_suite: bool
    case_count: Literal[3] = 3
    repetitions_per_case: Literal[2] = 2
    trial_count: Literal[6] = 6
    base_model_calls: Literal[42] = 42
    repair_model_call_allowance: Literal[12] = 12
    max_model_calls: Literal[54] = 54
    max_completion_tokens: Literal[55800] = 55800
    amap_calls_planned: Literal[0] = 0
    deepseek_key_configured: bool
    langsmith_key_configured: bool
    langsmith_tracing_enabled: bool
    ready_for_explicit_live_run: bool
    blocking_reasons: tuple[Identifier, ...]

    @model_validator(mode="after")
    def validate_preflight(self) -> "LiveComparisonPreflight":
        expected_ready = (
            self.deepseek_key_configured
            and self.langsmith_key_configured
            and self.langsmith_tracing_enabled
            and self.model_matches_suite
        )
        if self.ready_for_explicit_live_run != expected_ready:
            raise ValueError("live comparison readiness must match configured dependencies")
        if self.ready_for_explicit_live_run == bool(self.blocking_reasons):
            raise ValueError("live comparison blocking reasons must match readiness")
        return self


__all__ = [
    "LiveComparisonCallBudget",
    "LiveComparisonFairnessContract",
    "LiveComparisonPilotCase",
    "LiveComparisonPilotProfile",
    "LiveComparisonPilotSuite",
    "LiveComparisonPreflight",
]
