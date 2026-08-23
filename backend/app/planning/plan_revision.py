import hashlib
import json
from datetime import timedelta

from app.domain.planning import DayPlan, ItineraryItem, TripPlan
from app.domain.request import TripRequest
from app.planning.revision_contracts import (
    PlanRevisionDiff,
    PlanRevisionOperation,
    PlanRevisionRequest,
    PlanRevisionResult,
)
from app.planning.validator import validate_trip_plan


class PlanRevisionProtocolError(RuntimeError):
    """Raised when a structured revision exceeds its confirmed scope."""


def _plan_id(base_plan: TripPlan, request: PlanRevisionRequest, days: tuple[DayPlan, ...]) -> str:
    payload = {
        "base_plan_id": base_plan.plan_id,
        "revision": request.model_dump(mode="json"),
        "days": [day.model_dump(mode="json") for day in days],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"trip-plan-{digest}"


def _shift_item(item: ItineraryItem, *, minutes: int) -> ItineraryItem:
    shifted = item.model_copy(
        update={
            "start_at": item.start_at + timedelta(minutes=minutes),
            "end_at": item.end_at + timedelta(minutes=minutes),
        }
    )
    return ItineraryItem.model_validate(shifted.model_dump(mode="python"))


def apply_plan_revision(
    trip_request: TripRequest,
    base_plan: TripPlan,
    revision: PlanRevisionRequest,
) -> PlanRevisionResult:
    if revision.operation != PlanRevisionOperation.SHIFT_DAY_LATER:
        raise PlanRevisionProtocolError("unsupported plan revision operation")
    if revision.base_plan_id != base_plan.plan_id:
        raise PlanRevisionProtocolError("revision base plan does not match the current plan")
    if base_plan.request_id != trip_request.request_id:
        raise PlanRevisionProtocolError("revision plan does not belong to the current request")

    items_by_date = {day.date: day.items for day in base_plan.days}
    if revision.target_date not in items_by_date:
        raise PlanRevisionProtocolError("revision target date is outside the current plan")
    expected_target = tuple(item.item_id for item in items_by_date[revision.target_date])
    expected_protected = tuple(
        item.item_id
        for day in base_plan.days
        if day.date != revision.target_date
        for item in day.items
    )
    if revision.target_item_ids != expected_target:
        raise PlanRevisionProtocolError("revision target items do not match the current target day")
    if revision.protected_item_ids != expected_protected:
        raise PlanRevisionProtocolError("revision protected items do not match unaffected days")

    revised_days: list[DayPlan] = []
    for day in base_plan.days:
        if day.date != revision.target_date:
            revised_days.append(day)
            continue
        shifted_items = tuple(
            _shift_item(item, minutes=revision.shift_minutes) for item in day.items
        )
        if any(
            item.start_at.date() != day.date or item.end_at.date() != day.date
            for item in shifted_items
        ):
            raise PlanRevisionProtocolError("revision would move an item outside its target date")
        revised_days.append(day.model_copy(update={"items": shifted_items}))

    days = tuple(DayPlan.model_validate(day.model_dump(mode="python")) for day in revised_days)
    revised_plan = TripPlan.model_validate(
        base_plan.model_copy(
            update={
                "plan_id": _plan_id(base_plan, revision, days),
                "days": days,
            }
        ).model_dump(mode="python")
    )

    for before, after in zip(base_plan.days, revised_plan.days, strict=True):
        if before.date != revision.target_date and before != after:
            raise PlanRevisionProtocolError("revision changed a protected day")
    if (
        revised_plan.cost_items != base_plan.cost_items
        or revised_plan.weather_risks != base_plan.weather_risks
        or revised_plan.destination_city != base_plan.destination_city
        or revised_plan.start_date != base_plan.start_date
        or revised_plan.end_date != base_plan.end_date
    ):
        raise PlanRevisionProtocolError("revision changed protected plan facts")

    validation = validate_trip_plan(trip_request, revised_plan)
    return PlanRevisionResult(
        request=revision,
        revised_plan=revised_plan,
        validation=validation,
        diff=PlanRevisionDiff(
            from_plan_id=base_plan.plan_id,
            to_plan_id=revised_plan.plan_id,
            changed_dates=(revision.target_date,),
            rescheduled_item_ids=revision.target_item_ids,
        ),
    )
