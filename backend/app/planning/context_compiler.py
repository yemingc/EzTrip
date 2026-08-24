import hashlib
import json
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.domain.context import (
    BudgetPlanningContext,
    ClarificationKind,
    DestinationContext,
    PartyPlanningContext,
    PlannerCapability,
    PlannerContext,
    PlannerDayContext,
    PlannerReadiness,
    PlanningClarification,
)
from app.domain.money import BudgetCategory
from app.domain.request import ConstraintStrength, TripRequest

CENT = Decimal("0.01")

FIXTURE_CITY_DIRECTORY: dict[str, tuple[str, str]] = {
    "北京": ("北京市", "110000"),
    "北京市": ("北京市", "110000"),
    "上海": ("上海市", "310000"),
    "上海市": ("上海市", "310000"),
    "成都": ("成都市", "510100"),
    "成都市": ("成都市", "510100"),
}


def _request_sha256(request: TripRequest) -> str:
    serialized = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _money_reference(total: Decimal, divisor: int) -> Decimal:
    return (total / Decimal(divisor)).quantize(CENT, rounding=ROUND_HALF_UP)


def _destination_context(
    destination_city: str,
    destination_adcode: str | None = None,
) -> DestinationContext:
    input_name = destination_city.strip()
    if destination_adcode is not None:
        return DestinationContext(
            input_name=input_name,
            normalized_name=input_name,
            administrative_code=destination_adcode,
            primary_provider_supported=True,
        )
    resolved = FIXTURE_CITY_DIRECTORY.get(input_name)
    if resolved is None:
        return DestinationContext(
            input_name=input_name,
            normalized_name=input_name,
            primary_provider_supported=False,
        )
    normalized_name, administrative_code = resolved
    return DestinationContext(
        input_name=input_name,
        normalized_name=normalized_name,
        administrative_code=administrative_code,
        primary_provider_supported=True,
    )


def _budget_context(request: TripRequest) -> BudgetPlanningContext | None:
    budget = request.budget
    if budget is None:
        return None
    included = budget.included_categories
    included_set = set(included)
    excluded = tuple(category for category in BudgetCategory if category not in included_set)
    return BudgetPlanningContext(
        total_limit=budget.total_limit,
        hard_limit=budget.hard_limit,
        included_categories=included,
        excluded_categories=excluded,
        includes_lodging=budget.includes_lodging,
        reference_party_per_day=_money_reference(budget.total_limit, request.day_count),
        reference_per_traveler_trip=_money_reference(
            budget.total_limit,
            request.party.total_travelers,
        ),
        reference_per_traveler_day=_money_reference(
            budget.total_limit,
            request.party.total_travelers * request.day_count,
        ),
    )


def _constraint_clarification_id(constraint_id: str) -> str:
    digest = hashlib.sha256(constraint_id.encode("utf-8")).hexdigest()[:12]
    return f"clarify-constraint-{digest}"


def compile_planner_context(request: TripRequest) -> PlannerContext:
    """Compile validated user input into one deterministic planning interpretation."""

    request_sha256 = _request_sha256(request)
    destination = _destination_context(
        request.destination_city,
        request.destination_adcode,
    )
    budget = _budget_context(request)

    confirmed_hard = tuple(
        item
        for item in request.constraints.items
        if item.confirmed and item.strength == ConstraintStrength.HARD
    )
    confirmed_soft = tuple(
        item
        for item in request.constraints.items
        if item.confirmed and item.strength == ConstraintStrength.SOFT
    )
    pending = tuple(item for item in request.constraints.items if not item.confirmed)

    clarifications: list[PlanningClarification] = []
    blocked: set[PlannerCapability] = set()

    if not destination.primary_provider_supported:
        destination_affected = (
            PlannerCapability.CANDIDATE_SEARCH,
            PlannerCapability.STAY_SEARCH,
            PlannerCapability.WEATHER_LOOKUP,
            PlannerCapability.ROUTE_PLANNING,
            PlannerCapability.PLAN_FINALIZATION,
        )
        blocked.update(destination_affected)
        clarifications.append(
            PlanningClarification(
                clarification_id="clarify-destination-support",
                kind=ClarificationKind.UNSUPPORTED_DESTINATION,
                field_path="destination_city",
                prompt="目标城市尚未完成服务端解析, 请先确认城市候选。",
                reason="主地图 provider 需要已确认的行政区代码才能查询旅行数据。",
                affected_capabilities=destination_affected,
                blocking=True,
            )
        )

    if request.party.rooms is None:
        room_affected = [PlannerCapability.STAY_SEARCH]
        blocking = bool(budget and budget.includes_lodging)
        if blocking:
            room_affected.extend(
                (
                    PlannerCapability.BUDGET_VALIDATION,
                    PlannerCapability.PLAN_FINALIZATION,
                )
            )
        blocked.update(room_affected)
        clarifications.append(
            PlanningClarification(
                clarification_id="clarify-room-count",
                kind=ClarificationKind.MISSING_ROOMS,
                field_path="party.rooms",
                prompt="本次住宿需要几间房?",
                reason=("住宿晚数可以由日期确定, 但房间数不能根据人数静默推断。"),
                affected_capabilities=tuple(room_affected),
                blocking=blocking,
            )
        )

    if budget is None:
        blocked.add(PlannerCapability.BUDGET_VALIDATION)
        clarifications.append(
            PlanningClarification(
                clarification_id="clarify-budget",
                kind=ClarificationKind.MISSING_BUDGET,
                field_path="budget",
                prompt="是否需要设置本次行程的人民币总预算和覆盖类别?",
                reason="没有预算仍可搜索候选, 但不能声称计划满足预算。",
                affected_capabilities=(PlannerCapability.BUDGET_VALIDATION,),
                blocking=False,
            )
        )

    for constraint in pending:
        blocking = constraint.strength == ConstraintStrength.HARD
        if blocking:
            blocked.add(PlannerCapability.PLAN_FINALIZATION)
        clarifications.append(
            PlanningClarification(
                clarification_id=_constraint_clarification_id(constraint.constraint_id),
                kind=ClarificationKind.UNCONFIRMED_CONSTRAINT,
                field_path=f"constraints.{constraint.constraint_id}",
                prompt=f"请确认约束: {constraint.value}",
                reason=(
                    "未确认的硬约束不能进入最终计划。"
                    if blocking
                    else "未确认的偏好会保留, 但不会覆盖已确认条件。"
                ),
                affected_capabilities=(PlannerCapability.PLAN_FINALIZATION,),
                blocking=blocking,
                constraint_id=constraint.constraint_id,
            )
        )

    readiness = PlannerReadiness.READY
    if any(item.blocking for item in clarifications):
        readiness = PlannerReadiness.NEEDS_CLARIFICATION
    elif clarifications:
        readiness = PlannerReadiness.READY_WITH_QUESTIONS

    ready_capabilities = tuple(
        capability for capability in PlannerCapability if capability not in blocked
    )
    blocked_capabilities = tuple(
        capability for capability in PlannerCapability if capability in blocked
    )
    global_constraint_ids = tuple(
        item.constraint_id for item in request.constraints.items if not item.applies_to_dates
    )
    days = tuple(
        PlannerDayContext(
            day_number=offset + 1,
            date=request.start_date + timedelta(days=offset),
            constraint_ids=tuple(
                item.constraint_id
                for item in request.constraints.items
                if request.start_date + timedelta(days=offset) in item.applies_to_dates
            ),
        )
        for offset in range(request.day_count)
    )

    return PlannerContext(
        context_id=f"planner-context-{request_sha256[:16]}",
        request_id=request.request_id,
        input_request_sha256=request_sha256,
        origin_city=request.origin_city,
        destination=destination,
        start_date=request.start_date,
        end_date=request.end_date,
        day_count=request.day_count,
        lodging_nights=request.lodging_nights,
        party=PartyPlanningContext(
            adults=request.party.adults,
            children=request.party.children,
            seniors=request.party.seniors,
            total_travelers=request.party.total_travelers,
            rooms=request.party.rooms,
            lodging_nights=request.lodging_nights,
            room_nights=(
                request.party.rooms * request.lodging_nights
                if request.party.rooms is not None
                else None
            ),
        ),
        budget=budget,
        travel_styles=request.travel_styles,
        confirmed_hard_constraints=confirmed_hard,
        confirmed_soft_constraints=confirmed_soft,
        pending_constraints=pending,
        global_constraint_ids=global_constraint_ids,
        days=days,
        clarifications=tuple(clarifications),
        readiness=readiness,
        ready_capabilities=ready_capabilities,
        blocked_capabilities=blocked_capabilities,
    )
