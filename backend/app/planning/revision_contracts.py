from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier
from app.domain.planning import TripPlan
from app.domain.validation import PlanValidationReport
from app.planning.material_contracts import PlanningMaterialBundle


class PlanRevisionOperation(StrEnum):
    SHIFT_DAY_LATER = "shift_day_later"
    REPLACE_ACTIVITY = "replace_activity"


class PlanRevisionRequest(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    revision_id: Identifier
    base_version_id: Identifier
    base_plan_id: Identifier
    target_date: date
    operation: Literal[
        PlanRevisionOperation.SHIFT_DAY_LATER,
        PlanRevisionOperation.REPLACE_ACTIVITY,
    ]
    shift_minutes: int | None = Field(default=None, ge=30, le=180, multiple_of=30)
    replaced_item_id: Identifier | None = None
    replacement_candidate_id: Identifier | None = None
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
        if self.operation == PlanRevisionOperation.SHIFT_DAY_LATER:
            if (
                self.shift_minutes is None
                or self.replaced_item_id is not None
                or self.replacement_candidate_id is not None
            ):
                raise ValueError("shift-day-later requires only shift_minutes")
        elif (
            self.shift_minutes is not None
            or self.replaced_item_id is None
            or self.replacement_candidate_id is None
            or self.replaced_item_id not in self.target_item_ids
        ):
            raise ValueError("replace-activity requires a target item and replacement candidate")
        return self


class PlanRevisionDiff(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    from_plan_id: Identifier
    to_plan_id: Identifier
    changed_dates: tuple[date, ...] = Field(min_length=1)
    rescheduled_item_ids: tuple[Identifier, ...] = ()
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
    executor_version: Literal[
        "deterministic-local-revision-v1",
        "deterministic-local-revision-v2",
    ] = "deterministic-local-revision-v2"
    request: PlanRevisionRequest
    revised_plan: TripPlan
    validation: PlanValidationReport
    diff: PlanRevisionDiff
    revised_materials: PlanningMaterialBundle | None = None
    reused_provider_results: bool = True
    reused_planner_result: Literal[True] = True
    model_call_count: Literal[0] = 0
    provider_call_count: int = Field(default=0, ge=0, le=4)

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
        if self.request.operation == PlanRevisionOperation.SHIFT_DAY_LATER:
            if self.diff.rescheduled_item_ids != self.request.target_item_ids:
                raise ValueError("shift-day revision must reschedule exactly the target items")
            if (
                self.diff.added_item_ids
                or self.diff.removed_item_ids
                or self.revised_materials is not None
                or not self.reused_provider_results
                or self.provider_call_count
            ):
                raise ValueError("shift-day revision cannot replace items or call Providers")
        else:
            if (
                self.revised_materials is None
                or self.revised_materials.request_id != self.revised_plan.request_id
                or len(self.diff.added_item_ids) != 1
                or len(self.diff.removed_item_ids) != 1
                or self.reused_provider_results
                or self.provider_call_count < 1
            ):
                raise ValueError(
                    "activity replacement requires revised materials and one added/removed item"
                )
        return self
