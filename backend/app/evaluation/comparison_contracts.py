from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.validation import RepairAction
from app.evaluation.contracts import SeedTier
from app.planning.repair_contracts import RepairStopReason


class ComparisonArm(StrEnum):
    SINGLE_AGENT_TOOLS = "single_agent_tools"
    PRODUCT_GRAPH_NO_HARD_GATE = "product_graph_no_hard_gate"
    PRODUCT_GRAPH_BOUNDED_REPAIR = "product_graph_bounded_repair"


COMPARISON_ARMS = (
    ComparisonArm.SINGLE_AGENT_TOOLS,
    ComparisonArm.PRODUCT_GRAPH_NO_HARD_GATE,
    ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR,
)


class ComparisonScenario(StrEnum):
    CLEAN = "clean"
    MISSING_ROUTE = "missing_route"
    TIGHT_TRANSFER = "tight_transfer"
    OUTSIDE_OPENING_WINDOW = "outside_opening_window"
    MISSING_OPENING_EVIDENCE = "missing_opening_evidence"
    POI_CROSS_CITY = "poi_cross_city"
    STAY_CROSS_CITY = "stay_cross_city"
    ROUTE_SOURCE_MISMATCH = "route_source_mismatch"
    CANDIDATE_SOURCE_MISMATCH = "candidate_source_mismatch"
    HARD_BUDGET_INCOMPLETE = "hard_budget_incomplete"
    BUDGET_FLOOR_EXCEEDED = "budget_floor_exceeded"
    SCHEDULED_AVOID = "scheduled_avoid"
    MISSING_MUST_VISIT = "missing_must_visit"
    ROUTE_PROVIDER_TIMEOUT = "route_provider_timeout"
    UNSUPPORTED_CITY = "unsupported_city"
    OPENING_WINDOW_NO_FIT = "opening_window_no_fit"


class ComparisonOutcome(StrEnum):
    FINALIZABLE_WITHOUT_REPAIR = "finalizable_without_repair"
    REPAIRED = "repaired"
    WAITING_FOR_USER = "waiting_for_user"
    UNRESOLVED = "unresolved"
    BLOCKED_BEFORE_PLAN = "blocked_before_plan"


class TravelerProfile(StrEnum):
    ADULTS = "adults"
    COUPLE = "couple"
    FAMILY_WITH_CHILD = "family_with_child"
    UNSUPPORTED_BOUNDARY = "unsupported_boundary"


class ComparisonRiskDimension(StrEnum):
    GROUNDING = "grounding"
    HARD_CONSTRAINT = "hard_constraint"
    ROUTE = "route"
    OPENING_HOURS = "opening_hours"
    BUDGET = "budget"
    PROVIDER_FAILURE = "provider_failure"
    CAPABILITY_BOUNDARY = "capability_boundary"
    REPAIR_SCOPE = "repair_scope"
    HITL = "hitl"


class ComparisonFairnessContract(DomainModel):
    same_structured_request: Literal[True]
    same_provider_fixture: Literal[True]
    same_output_trip_plan_contract: Literal[True]
    same_post_run_evaluator: Literal[True]
    same_model_name_for_live_run: Literal[True]
    shared_case_eligibility_denominator: Literal[True]
    development_regression_not_holdout: Literal[True]


class ComparisonDimensions(DomainModel):
    city: NonEmptyText
    trip_days: int = Field(ge=2, le=5)
    traveler_profile: TravelerProfile
    risk_dimensions: tuple[ComparisonRiskDimension, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dimensions(self) -> "ComparisonDimensions":
        if len(self.risk_dimensions) != len(set(self.risk_dimensions)):
            raise ValueError("comparison risk dimensions must be unique")
        return self


class ComparisonExpectation(DomainModel):
    initial_error_codes: tuple[NonEmptyText, ...]
    full_outcome: ComparisonOutcome
    final_can_finalize: bool | None
    repair_actions: tuple[RepairAction, ...]
    stop_reason: RepairStopReason | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ComparisonExpectation":
        if len(self.initial_error_codes) != len(set(self.initial_error_codes)):
            raise ValueError("comparison initial errors must be unique")

        if self.full_outcome == ComparisonOutcome.BLOCKED_BEFORE_PLAN:
            if any(
                (
                    self.initial_error_codes,
                    self.final_can_finalize is not None,
                    self.repair_actions,
                    self.stop_reason is not None,
                )
            ):
                raise ValueError("blocked comparison outcomes cannot contain plan-stage results")
            return self

        if self.full_outcome == ComparisonOutcome.FINALIZABLE_WITHOUT_REPAIR:
            if (
                self.initial_error_codes
                or self.final_can_finalize is not True
                or self.repair_actions
                or self.stop_reason is not None
            ):
                raise ValueError("finalizable comparison outcomes cannot contain repair state")
            return self

        if not self.initial_error_codes or self.final_can_finalize is None:
            raise ValueError("post-validation comparison outcomes require initial errors")

        if self.full_outcome == ComparisonOutcome.REPAIRED:
            if (
                self.final_can_finalize is not True
                or not self.repair_actions
                or self.stop_reason != RepairStopReason.FINALIZABLE
            ):
                raise ValueError("repaired comparison outcomes require finalizable repair actions")
            return self

        if self.final_can_finalize is not False:
            raise ValueError("waiting and unresolved outcomes cannot finalize")
        if self.full_outcome == ComparisonOutcome.WAITING_FOR_USER:
            if (
                self.repair_actions
                or self.stop_reason != RepairStopReason.USER_CONFIRMATION_REQUIRED
            ):
                raise ValueError("waiting comparison outcomes require a confirmation stop")
            return self

        if self.stop_reason not in {
            RepairStopReason.UNREPAIRABLE_ISSUE,
            RepairStopReason.RETRY_LIMIT_REACHED,
        }:
            raise ValueError("unresolved comparison outcomes require an unresolved stop")
        return self


class ComparisonEvalCase(DomainModel):
    case_id: Identifier
    title: NonEmptyText
    tier: SeedTier
    source_plan_case_id: Identifier
    scenario: ComparisonScenario
    dimensions: ComparisonDimensions
    expected: ComparisonExpectation
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case(self) -> "ComparisonEvalCase":
        if not self.case_id.startswith("comparison-") or not self.case_id.endswith("-v1"):
            raise ValueError("comparison case_id must encode suite and version")
        return self


class ComparisonEvalSuite(DomainModel):
    suite: Literal["system-comparison-v1"] = "system-comparison-v1"
    version: Literal[1] = 1
    dataset_role: Literal["development_regression"] = "development_regression"
    arms: tuple[ComparisonArm, ...]
    fairness: ComparisonFairnessContract
    cases: tuple[ComparisonEvalCase, ...] = Field(min_length=30, max_length=30)

    @model_validator(mode="after")
    def validate_inventory(self) -> "ComparisonEvalSuite":
        if self.arms != COMPARISON_ARMS:
            raise ValueError("comparison arms and order must match the frozen protocol")
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise ValueError("comparison case ids must be unique")
        tier_counts = {tier: sum(item.tier == tier for item in self.cases) for tier in SeedTier}
        if tier_counts != {SeedTier.STANDARD: 20, SeedTier.HARD: 10}:
            raise ValueError("comparison inventory must contain 20 standard and 10 hard cases")
        if {item.scenario for item in self.cases} != set(ComparisonScenario):
            raise ValueError("comparison inventory must cover every scenario")
        outcome_counts = {
            outcome: sum(item.expected.full_outcome == outcome for item in self.cases)
            for outcome in ComparisonOutcome
        }
        if outcome_counts != {
            ComparisonOutcome.FINALIZABLE_WITHOUT_REPAIR: 4,
            ComparisonOutcome.REPAIRED: 16,
            ComparisonOutcome.WAITING_FOR_USER: 1,
            ComparisonOutcome.UNRESOLVED: 7,
            ComparisonOutcome.BLOCKED_BEFORE_PLAN: 2,
        }:
            raise ValueError("comparison outcome inventory must match the frozen protocol")
        return self


__all__ = [
    "COMPARISON_ARMS",
    "ComparisonArm",
    "ComparisonDimensions",
    "ComparisonEvalCase",
    "ComparisonEvalSuite",
    "ComparisonExpectation",
    "ComparisonFairnessContract",
    "ComparisonOutcome",
    "ComparisonRiskDimension",
    "ComparisonScenario",
    "TravelerProfile",
]
