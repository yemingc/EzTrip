import asyncio
import hashlib
from datetime import UTC, datetime, time, timedelta, timezone

from app.agents.plan_agent import run_plan_agent
from app.domain.opening_hours import OpeningHoursEvidence, OpeningHoursEvidenceBundle
from app.domain.planning import ActivityKind, DayPlan, ItineraryItem, TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import RiskSeverity, WeatherRisk, WeatherRiskType
from app.evaluation.plan_agent import (
    PlanAgentFixtureModel,
    build_plan_agent_materials,
    load_plan_agent_suite,
)
from app.planning.material_contracts import PlanningMaterialBundle, RouteEdgeStatus
from app.planning.weather_repair import run_weather_repair
from app.planning.weather_repair_contracts import (
    WeatherChangeGrade,
    WeatherImpact,
    WeatherRepairOutcome,
    WeatherRepairTask,
    WeatherReplanExecutionResult,
    WeatherReplanExecutionStatus,
)

SHANGHAI = timezone(timedelta(hours=8))


async def _base_fixture() -> tuple[TripRequest, PlanningMaterialBundle, TripPlan]:
    case = load_plan_agent_suite().cases[0]
    materials = await build_plan_agent_materials(case)
    result = run_plan_agent(case.request, materials, PlanAgentFixtureModel())
    assert result.plan is not None
    return case.request, materials, result.plan


def _risk(
    plan: TripPlan,
    *,
    severity: RiskSeverity = RiskSeverity.MEDIUM,
    activity_type: str = "outdoor",
    day_offset: int = 1,
    starts: time = time(13),
    ends: time = time(17),
) -> WeatherRisk:
    service_date = plan.start_date + timedelta(days=day_offset)
    starts_at = datetime.combine(service_date, starts, tzinfo=SHANGHAI)
    ends_at = datetime.combine(service_date, ends, tzinfo=SHANGHAI)
    material = f"{plan.plan_id}|{service_date}|{severity}|{activity_type}|{starts}|{ends}"
    return WeatherRisk(
        risk_id=f"weather-repair-{hashlib.sha256(material.encode()).hexdigest()[:12]}",
        city=plan.destination_city,
        starts_at=starts_at,
        ends_at=ends_at,
        risk_type=WeatherRiskType.RAIN,
        severity=severity,
        threshold_description="fixture 显著降雨风险。",
        affected_activity_types=(activity_type,),
        advisory="系统主动建议调整受影响的户外活动。",
        source=SourceReference(
            provider="weather-repair-test-fixture",
            provider_id=f"source-{hashlib.sha256(material.encode()).hexdigest()[:12]}",
            data_mode=DataMode.FIXTURE,
            retrieved_at=datetime(2026, 9, 21, tzinfo=UTC),
        ),
    )


def _opening_hours(request_id: str, plan: TripPlan) -> OpeningHoursEvidenceBundle:
    grounded = tuple(
        (day.date, item)
        for day in plan.days
        for item in day.items
        if item.kind in {ActivityKind.ATTRACTION, ActivityKind.MEAL}
        and item.candidate_id is not None
    )
    candidate_ids = tuple(dict.fromkeys(item.candidate_id for _, item in grounded))
    items = tuple(
        OpeningHoursEvidence(
            evidence_id=f"weather-opening-{candidate_id}-{service_date.isoformat()}",
            candidate_id=candidate_id,
            service_date=service_date,
            opens_at=datetime.combine(service_date, time(8), tzinfo=SHANGHAI),
            closes_at=datetime.combine(service_date, time(21), tzinfo=SHANGHAI),
            source=SourceReference(
                provider="weather-repair-opening-fixture",
                provider_id=f"opening-{candidate_id}-{service_date.isoformat()}",
                data_mode=DataMode.FIXTURE,
                retrieved_at=datetime(2026, 9, 21, tzinfo=UTC),
            ),
        )
        for candidate_id in candidate_ids
        for service_date in (day.date for day in plan.days)
    )
    return OpeningHoursEvidenceBundle(
        request_id=request_id,
        data_mode=DataMode.FIXTURE,
        items=items,
    )


def _replace_days(plan: TripPlan, days: tuple[DayPlan, ...]) -> TripPlan:
    return TripPlan.model_validate({**plan.model_dump(mode="python"), "days": days})


def _shift_item(item: ItineraryItem, delta: timedelta) -> ItineraryItem:
    return item.model_copy(
        update={"start_at": item.start_at + delta, "end_at": item.end_at + delta}
    )


class _Executor:
    def __init__(self, scenario: str, risk: WeatherRisk) -> None:
        self.scenario = scenario
        self.risk = risk
        self.calls = 0

    async def replan(
        self,
        request: TripRequest,
        plan: TripPlan,
        materials: PlanningMaterialBundle,
        opening_hours: OpeningHoursEvidenceBundle,
        task: WeatherRepairTask,
        impacts: tuple[WeatherImpact, ...],
        attempt_index: int,
    ) -> WeatherReplanExecutionResult:
        del request, opening_hours, task, attempt_index
        self.calls += 1
        if self.scenario == "failed":
            return WeatherReplanExecutionResult(
                status=WeatherReplanExecutionStatus.FAILED,
                error_code="weather-provider-replan-failed",
            )
        impacted_ids = {item.item_id for item in impacts}
        if self.scenario == "cross_day":
            source_day = next(
                day for day in plan.days if any(item.item_id in impacted_ids for item in day.items)
            )
            impacted = next(item for item in source_day.items if item.item_id in impacted_ids)
            target_date = source_day.date + timedelta(days=1)
            stay = materials.shortlist.primary_stay
            assert stay is not None and impacted.candidate_id is not None
            route = next(
                edge.route
                for edge in materials.route_matrix.edges
                if edge.origin_candidate_id == stay.candidate_id
                and edge.destination_candidate_id == impacted.candidate_id
                and edge.status == RouteEdgeStatus.SUCCEEDED
            )
            assert route is not None
            moved = impacted.model_copy(
                update={
                    "start_at": datetime.combine(target_date, time(14), tzinfo=SHANGHAI),
                    "end_at": datetime.combine(target_date, time(16), tzinfo=SHANGHAI),
                    "route_from_previous": route,
                }
            )
            days = []
            for day in plan.days:
                items = tuple(item for item in day.items if item.item_id != impacted.item_id)
                if day.date == target_date:
                    items = tuple(sorted((*items, moved), key=lambda item: item.start_at))
                days.append(day.model_copy(update={"items": items}))
            proposal = _replace_days(plan, tuple(days))
        else:
            days = []
            for day in plan.days:
                items = tuple(
                    _shift_item(item, timedelta(hours=3, minutes=30))
                    if item.item_id in impacted_ids
                    else item
                    for item in day.items
                )
                if self.scenario == "scope_violation" and day.date == plan.start_date:
                    items = tuple(
                        _shift_item(item, timedelta(minutes=15)) if index == 0 else item
                        for index, item in enumerate(items)
                    )
                days.append(
                    day.model_copy(
                        update={"items": tuple(sorted(items, key=lambda item: item.start_at))}
                    )
                )
            proposal = _replace_days(plan, tuple(days))
        return WeatherReplanExecutionResult(
            status=WeatherReplanExecutionStatus.SUCCEEDED,
            proposed_plan=proposal,
            model_call_count=1,
        )


def test_low_risk_does_not_create_a_replan_task() -> None:
    request, materials, plan = asyncio.run(_base_fixture())
    risk = _risk(plan, severity=RiskSeverity.LOW)
    executor = _Executor("minor", risk)
    result = asyncio.run(
        run_weather_repair(
            request,
            plan,
            materials,
            _opening_hours(request.request_id, plan),
            (risk,),
            executor,
        )
    )

    assert result.outcome == WeatherRepairOutcome.NO_ACTION
    assert result.task is None
    assert executor.calls == 0


def test_significant_overlap_auto_applies_only_a_minor_local_change() -> None:
    request, materials, plan = asyncio.run(_base_fixture())
    risk = _risk(plan)
    executor = _Executor("minor", risk)
    result = asyncio.run(
        run_weather_repair(
            request,
            plan,
            materials,
            _opening_hours(request.request_id, plan),
            (risk,),
            executor,
        )
    )

    assert result.outcome == WeatherRepairOutcome.AUTO_APPLIED
    assert result.change.grade == WeatherChangeGrade.MINOR
    assert result.change.diff.changed_dates == (plan.start_date + timedelta(days=1),)
    assert not result.requires_user_confirmation
    assert result.attempts[0].remaining_impact_count == 0


def test_cross_day_move_is_a_pending_hitl_proposal() -> None:
    request, materials, plan = asyncio.run(_base_fixture())
    risk = _risk(plan)
    result = asyncio.run(
        run_weather_repair(
            request,
            plan,
            materials,
            _opening_hours(request.request_id, plan),
            (risk,),
            _Executor("cross_day", risk),
        )
    )

    assert result.outcome == WeatherRepairOutcome.WAITING_FOR_USER
    assert result.change.grade == WeatherChangeGrade.MAJOR
    assert "cross_day_move" in result.change.major_reasons
    assert result.requires_user_confirmation
    assert result.effective_plan == plan
    assert result.proposed_plan is not None


def test_unrelated_date_change_is_rejected_twice_without_mutating_effective_plan() -> None:
    request, materials, plan = asyncio.run(_base_fixture())
    risk = _risk(plan)
    executor = _Executor("scope_violation", risk)
    result = asyncio.run(
        run_weather_repair(
            request,
            plan,
            materials,
            _opening_hours(request.request_id, plan),
            (risk,),
            executor,
        )
    )

    assert result.outcome == WeatherRepairOutcome.UNRESOLVED
    assert executor.calls == 2
    assert all(not item.scope_valid for item in result.attempts)
    assert result.effective_plan == plan


def test_outdoor_label_does_not_match_an_indoor_only_time_window() -> None:
    request, materials, plan = asyncio.run(_base_fixture())
    risk = _risk(plan, day_offset=0, starts=time(8), ends=time(12))
    executor = _Executor("minor", risk)
    result = asyncio.run(
        run_weather_repair(
            request,
            plan,
            materials,
            _opening_hours(request.request_id, plan),
            (risk,),
            executor,
        )
    )

    assert result.outcome == WeatherRepairOutcome.NO_ACTION
    assert executor.calls == 0
