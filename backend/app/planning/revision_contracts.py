from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier
from app.domain.planning import TripPlan
from app.domain.validation import PlanValidationReport


class PlanRevisionOperation(StrEnum):
    SHIFT_DAY_LATER = "shift_day_later"


class PlanRevisionRequest(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    revision_id: Identifier
    base_version_id: Identifier
    base_plan_id: Identifier
    target_date: date
    operation: Literal[PlanRevisionOperation.SHIFT_DAY_LATER]
    shift_minutes: int = Field(ge=30, le=180, multiple_of=30)
    target_item_ids: tuple[Identifier, ...] = Field(min_length=1)
    protected_item_ids: tuple[Identifier, ...] = ()
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_scope(self) -> "PlanRevisionRequest":
        for values in (self.target_item_ids, self.protected_item_ids):
            if len(values) != len(set(values)):
                raise ValueError("revision item scopes must contain unique ids")
        if set(self.target_item_ids) & set(self.protected_item_ids):
            raise ValueError("revision target and protected items cannot overlap")
        return self


class PlanRevisionDiff(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    from_plan_id: Identifier
    to_plan_id: Identifier
    changed_dates: tuple[date, ...] = Field(min_length=1)
    rescheduled_item_ids: tuple[Identifier, ...] = Field(min_length=1)
    added_item_ids: tuple[Identifier, ...] = ()
    removed_item_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_diff(self) -> "PlanRevisionDiff":
        for values in (
            self.changed_dates,
            self.rescheduled_item_ids,
            self.added_item_ids,
            self.removed_item_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("revision diff collections must contain unique values")
        if self.from_plan_id == self.to_plan_id:
            raise ValueError("a revision must create a new plan id")
        if set(self.added_item_ids) & set(self.removed_item_ids):
            raise ValueError("a revision item cannot be both added and removed")
        return self


class PlanRevisionResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    executor_version: Literal["deterministic-local-revision-v1"] = "deterministic-local-revision-v1"
    request: PlanRevisionRequest
    revised_plan: TripPlan
    validation: PlanValidationReport
    diff: PlanRevisionDiff
    reused_provider_results: Literal[True] = True
    reused_planner_result: Literal[True] = True
    model_call_count: Literal[0] = 0
    provider_call_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_result(self) -> "PlanRevisionResult":
        if self.request.base_plan_id != self.diff.from_plan_id:
            raise ValueError("revision diff must start from the requested base plan")
        if self.revised_plan.plan_id != self.diff.to_plan_id:
            raise ValueError("revision diff must end at the revised plan")
        if (
            self.validation.request_id != self.revised_plan.request_id
            or self.validation.plan_id != self.revised_plan.plan_id
        ):
            raise ValueError("revision validation must describe the revised plan")
        if self.diff.changed_dates != (self.request.target_date,):
            raise ValueError("local revision may only change the requested target date")
        if self.diff.rescheduled_item_ids != self.request.target_item_ids:
            raise ValueError("revision diff must reschedule exactly the target items")
        if self.diff.added_item_ids or self.diff.removed_item_ids:
            raise ValueError("shift-day-later cannot add or remove itinerary items")
        return self
