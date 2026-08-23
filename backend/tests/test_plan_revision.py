import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.domain.planning import DayPlan, ItineraryItem, TripPlan
from app.evaluation import load_vertical_slice_suite, run_vertical_slice_case
from app.planning import (
    PlanRevisionOperation,
    PlanRevisionProtocolError,
    PlanRevisionRequest,
    apply_plan_revision,
)


def _normal_case_result():
    case = next(
        item for item in load_vertical_slice_suite().cases if item.expected.outcome == "ready"
    )
    result, _ = asyncio.run(run_vertical_slice_case(case))
    return case, result


def _revision(result, *, shift_minutes: int = 120) -> PlanRevisionRequest:
    target_day = result.plan.days[1]
    return PlanRevisionRequest(
        revision_id="revision-day-two-later-v1",
        base_version_id="plan-version-fixture-v1",
        base_plan_id=result.plan.plan_id,
        target_date=target_day.date,
        operation=PlanRevisionOperation.SHIFT_DAY_LATER,
        shift_minutes=shift_minutes,
        target_item_ids=tuple(item.item_id for item in target_day.items),
        protected_item_ids=tuple(
            item.item_id
            for day in result.plan.days
            if day.date != target_day.date
            for item in day.items
        ),
        confirmed=True,
    )


def test_structured_revision_changes_only_target_day_and_revalidates() -> None:
    case, result = _normal_case_result()
    revision = _revision(result)

    revised = apply_plan_revision(case.request, result.plan, revision)

    assert revised.revised_plan.plan_id != result.plan.plan_id
    assert revised.diff.changed_dates == (result.plan.days[1].date,)
    assert revised.diff.rescheduled_item_ids == revision.target_item_ids
    assert revised.revised_plan.days[0] == result.plan.days[0]
    assert revised.revised_plan.days[2] == result.plan.days[2]
    assert revised.revised_plan.days[1].items[0].start_at == (
        result.plan.days[1].items[0].start_at + timedelta(hours=2)
    )
    assert revised.validation.plan_id == revised.revised_plan.plan_id
    assert revised.validation.can_finalize is True
    assert revised.model_call_count == 0
    assert revised.provider_call_count == 0


def test_revision_rejects_stale_base_and_scope_drift() -> None:
    case, result = _normal_case_result()
    revision = _revision(result)

    with pytest.raises(PlanRevisionProtocolError, match="base plan"):
        apply_plan_revision(
            case.request,
            result.plan,
            revision.model_copy(update={"base_plan_id": "trip-plan-stale"}),
        )
    with pytest.raises(PlanRevisionProtocolError, match="protected items"):
        apply_plan_revision(
            case.request,
            result.plan,
            revision.model_copy(update={"protected_item_ids": ()}),
        )


def test_revision_contract_rejects_unconfirmed_or_overlapping_scope() -> None:
    _, result = _normal_case_result()
    revision = _revision(result)
    missing_confirmation = revision.model_dump(mode="json")
    del missing_confirmation["confirmed"]
    with pytest.raises(ValidationError):
        PlanRevisionRequest.model_validate(missing_confirmation)
    with pytest.raises(ValidationError):
        PlanRevisionRequest.model_validate(
            {
                **revision.model_dump(mode="json"),
                "confirmed": False,
            }
        )
    with pytest.raises(ValidationError, match="cannot overlap"):
        PlanRevisionRequest.model_validate(
            {
                **revision.model_dump(mode="json"),
                "protected_item_ids": [revision.target_item_ids[0]],
            }
        )


def test_revision_rejects_a_shift_that_crosses_the_target_date() -> None:
    case, result = _normal_case_result()
    target_day = result.plan.days[1]
    late_item = ItineraryItem.model_validate(
        target_day.items[0]
        .model_copy(
            update={
                "start_at": target_day.items[0].start_at.replace(hour=22),
                "end_at": target_day.items[0].end_at.replace(hour=23),
            }
        )
        .model_dump(mode="python")
    )
    late_day = DayPlan.model_validate(
        target_day.model_copy(update={"items": (late_item,)}).model_dump(mode="python")
    )
    late_plan = TripPlan.model_validate(
        result.plan.model_copy(
            update={
                "plan_id": "trip-plan-late-fixture",
                "days": (result.plan.days[0], late_day, result.plan.days[2]),
            }
        ).model_dump(mode="python")
    )
    late_result = result.model_copy(update={"plan": late_plan})

    with pytest.raises(PlanRevisionProtocolError, match="outside its target date"):
        apply_plan_revision(case.request, late_plan, _revision(late_result, shift_minutes=120))
