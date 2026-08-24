from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.candidates import CandidatePOI
from app.domain.money import CostItem
from app.domain.sources import SourceReference
from app.domain.travel_data import RouteLeg, WeatherRisk


class ActivityKind(StrEnum):
    ATTRACTION = "attraction"
    MEAL = "meal"
    STAY = "stay"
    TRANSIT = "transit"
    FREE_TIME = "free_time"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    PENDING_CONFIRMATION = "pending_confirmation"
    FINAL = "final"
    CONFLICTED = "conflicted"


class MealRecommendation(DomainModel):
    recommendation_id: Identifier
    anchor_candidate_id: Identifier
    candidate: CandidatePOI
    straight_line_distance_meters: int = Field(ge=0, le=5000)
    reason: NonEmptyText

    @model_validator(mode="after")
    def validate_recommendation(self) -> "MealRecommendation":
        if self.anchor_candidate_id == self.candidate.candidate_id:
            raise ValueError("meal recommendation must differ from its activity anchor")
        return self


class ItineraryItem(DomainModel):
    item_id: Identifier
    kind: ActivityKind
    title: NonEmptyText
    start_at: AwareDatetime
    end_at: AwareDatetime
    candidate_id: Identifier | None = None
    source: SourceReference | None = None
    route_from_previous: RouteLeg | None = None
    cost_item_ids: tuple[Identifier, ...] = ()
    notes: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_item(self) -> "ItineraryItem":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        grounded_kinds = {ActivityKind.ATTRACTION, ActivityKind.MEAL, ActivityKind.STAY}
        if self.kind in grounded_kinds:
            if self.candidate_id is None or self.source is None:
                raise ValueError("attraction, meal, and stay items require candidate_id and source")
            if self.source.provider_id is None:
                raise ValueError("grounded itinerary items require source.provider_id")
        if len(self.cost_item_ids) != len(set(self.cost_item_ids)):
            raise ValueError("cost_item_ids must be unique within an itinerary item")
        return self


class DayPlan(DomainModel):
    date: date
    items: tuple[ItineraryItem, ...] = Field(min_length=1)
    departure_from_stay_at: AwareDatetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    meal_recommendations: tuple[MealRecommendation, ...] = Field(
        default=(),
        max_length=3,
        exclude_if=lambda value: not value,
    )
    weather_risk_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_timeline(self) -> "DayPlan":
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("item_id values must be unique within a day")
        if len(self.weather_risk_ids) != len(set(self.weather_risk_ids)):
            raise ValueError("weather_risk_ids must be unique within a day")
        meal_ids = [item.recommendation_id for item in self.meal_recommendations]
        if len(meal_ids) != len(set(meal_ids)):
            raise ValueError("meal recommendation ids must be unique within a day")
        meal_candidate_ids = [item.candidate.candidate_id for item in self.meal_recommendations]
        if len(meal_candidate_ids) != len(set(meal_candidate_ids)):
            raise ValueError("meal recommendation candidates must be unique within a day")
        if any(
            item.start_at.date() != self.date or item.end_at.date() != self.date
            for item in self.items
        ):
            raise ValueError("all itinerary item timestamps must fall on the DayPlan date")
        if self.departure_from_stay_at is not None:
            if self.departure_from_stay_at.date() != self.date:
                raise ValueError("departure_from_stay_at must fall on the DayPlan date")
            if self.departure_from_stay_at > self.items[0].start_at:
                raise ValueError("departure_from_stay_at cannot be after the first activity")
        for previous, current in zip(self.items, self.items[1:], strict=False):
            if current.start_at < previous.start_at:
                raise ValueError("DayPlan items must be sorted by start_at")
            if current.start_at < previous.end_at:
                raise ValueError("DayPlan items must not overlap")
        return self


class TripPlan(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: Identifier
    request_id: Identifier
    status: PlanStatus
    destination_city: NonEmptyText
    start_date: date
    end_date: date
    days: tuple[DayPlan, ...] = Field(min_length=2, max_length=5)
    cost_items: tuple[CostItem, ...] = ()
    weather_risks: tuple[WeatherRisk, ...] = ()

    @model_validator(mode="after")
    def validate_plan(self) -> "TripPlan":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        expected_dates = [
            self.start_date + timedelta(days=offset)
            for offset in range((self.end_date - self.start_date).days + 1)
        ]
        actual_dates = [day.date for day in self.days]
        if actual_dates != expected_dates:
            raise ValueError("days must cover every trip date exactly once and in order")

        all_item_ids = [item.item_id for day in self.days for item in day.items]
        if len(all_item_ids) != len(set(all_item_ids)):
            raise ValueError("item_id values must be unique across the trip")

        meal_ids = [
            item.recommendation_id for day in self.days for item in day.meal_recommendations
        ]
        if len(meal_ids) != len(set(meal_ids)):
            raise ValueError("meal recommendation ids must be unique across the trip")

        cost_ids = [item.cost_item_id for item in self.cost_items]
        if len(cost_ids) != len(set(cost_ids)):
            raise ValueError("cost_item_id values must be unique across the trip")
        referenced_cost_ids = {
            cost_id for day in self.days for item in day.items for cost_id in item.cost_item_ids
        }
        missing_cost_ids = referenced_cost_ids - set(cost_ids)
        if missing_cost_ids:
            missing = ", ".join(sorted(missing_cost_ids))
            raise ValueError(f"itinerary items reference unknown cost items: {missing}")

        weather_risk_ids = [risk.risk_id for risk in self.weather_risks]
        if len(weather_risk_ids) != len(set(weather_risk_ids)):
            raise ValueError("risk_id values must be unique across the trip")
        if any(risk.city != self.destination_city for risk in self.weather_risks):
            raise ValueError("weather risks must match the trip destination city")
        if any(
            risk.ends_at.date() < self.start_date or risk.starts_at.date() > self.end_date
            for risk in self.weather_risks
        ):
            raise ValueError("weather risks must overlap the trip date range")
        referenced_risk_ids = {risk_id for day in self.days for risk_id in day.weather_risk_ids}
        missing_risk_ids = referenced_risk_ids - set(weather_risk_ids)
        if missing_risk_ids:
            missing = ", ".join(sorted(missing_risk_ids))
            raise ValueError(f"day plans reference unknown weather risks: {missing}")
        return self

    @property
    def total_cost_minimum(self) -> Decimal:
        return sum((item.total_minimum for item in self.cost_items), start=Decimal("0"))

    @property
    def total_cost_maximum(self) -> Decimal:
        return sum((item.total_maximum for item in self.cost_items), start=Decimal("0"))


class PlanVersion(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    version_id: Identifier
    plan: TripPlan
    version_number: int = Field(ge=1)
    based_on_version_id: Identifier | None = None
    created_at: AwareDatetime
    input_constraint_sha256: Sha256Digest
    tool_snapshot_ids: tuple[Identifier, ...]
    model_versions: dict[NonEmptyText, NonEmptyText]
    prompt_versions: dict[NonEmptyText, NonEmptyText]
    change_summary: tuple[NonEmptyText, ...] = Field(min_length=1)
    changed_dates: tuple[date, ...] = ()

    @model_validator(mode="after")
    def validate_version_lineage(self) -> "PlanVersion":
        if self.version_number == 1 and self.based_on_version_id is not None:
            raise ValueError("the first version cannot have a parent version")
        if self.version_number > 1 and self.based_on_version_id is None:
            raise ValueError("later versions require based_on_version_id")
        if len(self.changed_dates) != len(set(self.changed_dates)):
            raise ValueError("changed_dates must not contain duplicates")
        if any(not self.plan.start_date <= day <= self.plan.end_date for day in self.changed_dates):
            raise ValueError("changed_dates must fall within the plan date range")
        return self
