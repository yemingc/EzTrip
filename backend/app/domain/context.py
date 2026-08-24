from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.money import BudgetCategory
from app.domain.request import Constraint, TripPace


class PlannerReadiness(StrEnum):
    READY = "ready"
    READY_WITH_QUESTIONS = "ready_with_questions"
    NEEDS_CLARIFICATION = "needs_clarification"


class PlannerCapability(StrEnum):
    CANDIDATE_SEARCH = "candidate_search"
    STAY_SEARCH = "stay_search"
    WEATHER_LOOKUP = "weather_lookup"
    ROUTE_PLANNING = "route_planning"
    BUDGET_VALIDATION = "budget_validation"
    PLAN_FINALIZATION = "plan_finalization"


class ClarificationKind(StrEnum):
    UNSUPPORTED_DESTINATION = "unsupported_destination"
    MISSING_ROOMS = "missing_rooms"
    MISSING_BUDGET = "missing_budget"
    UNCONFIRMED_CONSTRAINT = "unconfirmed_constraint"


class DestinationContext(DomainModel):
    input_name: NonEmptyText
    normalized_name: NonEmptyText
    administrative_code: str | None = Field(default=None, pattern=r"^\d{6}$")
    primary_provider_supported: bool

    @model_validator(mode="after")
    def validate_support_fields(self) -> "DestinationContext":
        if self.primary_provider_supported != (self.administrative_code is not None):
            raise ValueError("provider support and administrative_code must agree")
        return self


class PartyPlanningContext(DomainModel):
    adults: int = Field(ge=0, le=20)
    children: int = Field(ge=0, le=20)
    seniors: int = Field(ge=0, le=20)
    total_travelers: int = Field(ge=1, le=60)
    rooms: int | None = Field(default=None, ge=1, le=20)
    lodging_nights: int = Field(ge=1, le=4)
    room_nights: int | None = Field(default=None, ge=1, le=80)

    @model_validator(mode="after")
    def validate_derived_party_values(self) -> "PartyPlanningContext":
        if self.total_travelers != self.adults + self.children + self.seniors:
            raise ValueError("total_travelers must equal the party composition")
        expected_room_nights = self.rooms * self.lodging_nights if self.rooms is not None else None
        if self.room_nights != expected_room_nights:
            raise ValueError("room_nights must equal rooms multiplied by lodging_nights")
        return self


class BudgetPlanningContext(DomainModel):
    total_limit: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: Literal["CNY"] = "CNY"
    hard_limit: bool
    included_categories: tuple[BudgetCategory, ...] = Field(min_length=1)
    excluded_categories: tuple[BudgetCategory, ...]
    includes_lodging: bool
    reference_party_per_day: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reference_per_traveler_trip: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reference_per_traveler_day: Decimal = Field(gt=0, max_digits=12, decimal_places=2)

    @model_validator(mode="after")
    def validate_budget_categories(self) -> "BudgetPlanningContext":
        included = set(self.included_categories)
        excluded = set(self.excluded_categories)
        if included & excluded:
            raise ValueError("included and excluded budget categories cannot overlap")
        if included | excluded != set(BudgetCategory):
            raise ValueError("included and excluded categories must cover every budget category")
        if self.includes_lodging != (BudgetCategory.LODGING in included):
            raise ValueError("includes_lodging must match included_categories")
        return self


class PlannerDayContext(DomainModel):
    day_number: int = Field(ge=1, le=5)
    date: date
    constraint_ids: tuple[Identifier, ...] = ()


class PlanningClarification(DomainModel):
    clarification_id: Identifier
    kind: ClarificationKind
    field_path: NonEmptyText
    prompt: NonEmptyText
    reason: NonEmptyText
    affected_capabilities: tuple[PlannerCapability, ...] = Field(min_length=1)
    blocking: bool
    constraint_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_constraint_reference(self) -> "PlanningClarification":
        if (self.kind == ClarificationKind.UNCONFIRMED_CONSTRAINT) != (
            self.constraint_id is not None
        ):
            raise ValueError("only constraint clarifications carry constraint_id")
        if len(self.affected_capabilities) != len(set(self.affected_capabilities)):
            raise ValueError("affected_capabilities must not contain duplicates")
        return self


class PlannerContext(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    compiler_version: Literal["planner-context-v1"] = "planner-context-v1"
    context_id: Identifier
    request_id: Identifier
    input_request_sha256: Sha256Digest
    locale: Literal["zh-CN"] = "zh-CN"
    origin_city: NonEmptyText | None = None
    destination: DestinationContext
    start_date: date
    end_date: date
    day_count: int = Field(ge=2, le=5)
    lodging_nights: int = Field(ge=1, le=4)
    party: PartyPlanningContext
    budget: BudgetPlanningContext | None = None
    pace: TripPace | None = Field(default=None, exclude_if=lambda value: value is None)
    travel_styles: tuple[NonEmptyText, ...] = ()
    confirmed_hard_constraints: tuple[Constraint, ...] = ()
    confirmed_soft_constraints: tuple[Constraint, ...] = ()
    pending_constraints: tuple[Constraint, ...] = ()
    global_constraint_ids: tuple[Identifier, ...] = ()
    days: tuple[PlannerDayContext, ...] = Field(min_length=2, max_length=5)
    clarifications: tuple[PlanningClarification, ...] = ()
    readiness: PlannerReadiness
    ready_capabilities: tuple[PlannerCapability, ...]
    blocked_capabilities: tuple[PlannerCapability, ...]

    @model_validator(mode="after")
    def validate_compiled_context(self) -> "PlannerContext":
        expected_day_count = (self.end_date - self.start_date).days + 1
        if self.day_count != expected_day_count:
            raise ValueError("day_count must match the inclusive date range")
        if self.lodging_nights != self.day_count - 1:
            raise ValueError("lodging_nights must equal day_count minus one")
        if self.party.lodging_nights != self.lodging_nights:
            raise ValueError("party lodging_nights must match the trip")

        expected_dates = tuple(
            self.start_date + timedelta(days=offset) for offset in range(self.day_count)
        )
        if tuple(day.date for day in self.days) != expected_dates:
            raise ValueError("days must cover the trip dates exactly once and in order")
        if tuple(day.day_number for day in self.days) != tuple(range(1, self.day_count + 1)):
            raise ValueError("day_number values must be contiguous and one-based")

        all_constraints = (
            *self.confirmed_hard_constraints,
            *self.confirmed_soft_constraints,
            *self.pending_constraints,
        )
        constraint_ids = [item.constraint_id for item in all_constraints]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("constraints must appear in exactly one confirmation bucket")
        constraints_by_id = {item.constraint_id: item for item in all_constraints}
        expected_global_ids = {
            item.constraint_id for item in all_constraints if not item.applies_to_dates
        }
        if set(self.global_constraint_ids) != expected_global_ids:
            raise ValueError("global_constraint_ids must match constraints without date scope")
        for day in self.days:
            expected_ids = {
                item.constraint_id for item in all_constraints if day.date in item.applies_to_dates
            }
            if set(day.constraint_ids) != expected_ids:
                raise ValueError("daily constraint_ids must match applies_to_dates")
            if len(day.constraint_ids) != len(set(day.constraint_ids)):
                raise ValueError("daily constraint_ids must not contain duplicates")
        if any(item not in constraints_by_id for item in self.global_constraint_ids):
            raise ValueError("global_constraint_ids reference unknown constraints")

        ready = set(self.ready_capabilities)
        blocked = set(self.blocked_capabilities)
        if ready & blocked:
            raise ValueError("ready and blocked capabilities cannot overlap")
        if ready | blocked != set(PlannerCapability):
            raise ValueError("capability partitions must cover every planner capability")
        if len(self.ready_capabilities) != len(ready) or len(self.blocked_capabilities) != len(
            blocked
        ):
            raise ValueError("capability lists must not contain duplicates")

        expected_readiness = PlannerReadiness.READY
        if any(item.blocking for item in self.clarifications):
            expected_readiness = PlannerReadiness.NEEDS_CLARIFICATION
        elif self.clarifications:
            expected_readiness = PlannerReadiness.READY_WITH_QUESTIONS
        if self.readiness != expected_readiness:
            raise ValueError("readiness must match clarification severity")
        return self
