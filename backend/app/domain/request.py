from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.money import BudgetCategory


class ConstraintStrength(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class ConstraintSource(StrEnum):
    USER_EXPLICIT = "user_explicit"
    USER_CONFIRMED = "user_confirmed"
    AGENT_INFERRED = "agent_inferred"
    SYSTEM = "system"


class ConstraintKind(StrEnum):
    MUST_VISIT = "must_visit"
    AVOID = "avoid"
    INTEREST = "interest"
    WALKING_INTENSITY = "walking_intensity"
    TRANSPORT_MODE = "transport_mode"
    ACCOMMODATION = "accommodation"
    MEAL = "meal"
    ACCESSIBILITY = "accessibility"
    SCHEDULE = "schedule"


ConstraintValue = str | int | float | bool | list[str]


class Party(DomainModel):
    adults: int = Field(ge=0, le=20)
    children: int = Field(default=0, ge=0, le=20)
    seniors: int = Field(default=0, ge=0, le=20)
    rooms: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def validate_party(self) -> "Party":
        if self.total_travelers == 0:
            raise ValueError("party must contain at least one traveler")
        if self.adults + self.seniors == 0:
            raise ValueError("a party containing children must include an adult or senior")
        if self.rooms is not None and self.rooms > self.total_travelers:
            raise ValueError("rooms cannot exceed total travelers")
        return self

    @property
    def total_travelers(self) -> int:
        return self.adults + self.children + self.seniors


class BudgetConstraint(DomainModel):
    total_limit: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: Literal["CNY"] = "CNY"
    scope: Literal["party_total"] = "party_total"
    period: Literal["whole_trip"] = "whole_trip"
    included_categories: tuple[BudgetCategory, ...] = Field(min_length=1)
    hard_limit: bool = True

    @model_validator(mode="after")
    def validate_categories_are_unique(self) -> "BudgetConstraint":
        if len(set(self.included_categories)) != len(self.included_categories):
            raise ValueError("included_categories must not contain duplicates")
        return self

    @property
    def includes_lodging(self) -> bool:
        return BudgetCategory.LODGING in self.included_categories


class Constraint(DomainModel):
    constraint_id: Identifier
    kind: ConstraintKind
    value: ConstraintValue
    strength: ConstraintStrength
    priority: int = Field(ge=1, le=5)
    source: ConstraintSource
    applies_to_dates: tuple[date, ...] = ()
    confirmed: bool


class ConstraintSet(DomainModel):
    items: tuple[Constraint, ...] = ()

    @model_validator(mode="after")
    def validate_items(self) -> "ConstraintSet":
        ids = [item.constraint_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("constraint_id values must be unique")

        hard_must_visit = {
            item.value.casefold()
            for item in self.items
            if item.kind == ConstraintKind.MUST_VISIT
            and item.strength == ConstraintStrength.HARD
            and isinstance(item.value, str)
        }
        hard_avoid = {
            item.value.casefold()
            for item in self.items
            if item.kind == ConstraintKind.AVOID
            and item.strength == ConstraintStrength.HARD
            and isinstance(item.value, str)
        }
        conflicts = hard_must_visit & hard_avoid
        if conflicts:
            conflict_list = ", ".join(sorted(conflicts))
            raise ValueError(f"hard must_visit and avoid constraints conflict: {conflict_list}")
        return self


class TripRequest(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: Identifier
    locale: Literal["zh-CN"] = "zh-CN"
    raw_text: NonEmptyText
    origin_city: NonEmptyText | None = None
    destination_city: NonEmptyText
    destination_adcode: str | None = Field(
        default=None,
        pattern=r"^\d{6}$",
        exclude_if=lambda value: value is None,
    )
    start_date: date
    end_date: date
    party: Party
    budget: BudgetConstraint | None = None
    travel_styles: tuple[NonEmptyText, ...] = ()
    constraints: ConstraintSet = Field(default_factory=ConstraintSet)

    @model_validator(mode="after")
    def validate_v1_trip_window(self) -> "TripRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if not 2 <= self.day_count <= 5:
            raise ValueError("V1 supports single-city trips lasting 2 to 5 calendar days")
        if len(self.travel_styles) != len(set(self.travel_styles)):
            raise ValueError("travel_styles must not contain duplicates")
        constraint_dates = {
            constraint_date
            for constraint in self.constraints.items
            for constraint_date in constraint.applies_to_dates
        }
        if any(not self.start_date <= day <= self.end_date for day in constraint_dates):
            raise ValueError("constraint dates must fall within the trip date range")
        return self

    @property
    def day_count(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def lodging_nights(self) -> int:
        return self.day_count - 1
