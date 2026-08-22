import hashlib
import json
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path

from app.agents.plan_agent import run_plan_agent
from app.domain.opening_hours import OpeningHoursEvidence, OpeningHoursEvidenceBundle
from app.domain.planning import ActivityKind, DayPlan, ItineraryItem, TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import RiskSeverity, WeatherRisk, WeatherRiskType
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.plan_agent import (
    PlanAgentFixtureModel,
    build_plan_agent_materials,
    load_plan_agent_suite,
)
from app.evaluation.weather_repair_contracts import (
    WeatherRepairBaselineReport,
    WeatherRepairCaseResult,
    WeatherRepairEvalCase,
    WeatherRepairEvalSuite,
    WeatherRepairScenario,
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

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WEATHER_REPAIR_SUITE_PATH = REPOSITORY_ROOT / "evals" / "cases" / "weather-repair" / "suite.v1.json"
CN_TIMEZONE = timezone(timedelta(hours=8))


class WeatherRepairEvaluationError(RuntimeError):
    """Raised when a Weather Repair fixture contradicts its references."""


def load_weather_repair_suite(
    suite_path: Path = WEATHER_REPAIR_SUITE_PATH,
) -> WeatherRepairEvalSuite:
    return WeatherRepairEvalSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))


def weather_repair_dataset_sha256(suite: WeatherRepairEvalSuite) -> str:
    canonical = json.dumps(
        suite.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _base_fixture() -> tuple[TripRequest, PlanningMaterialBundle, TripPlan]:
    case = load_plan_agent_suite().cases[0]
    materials = await build_plan_agent_materials(case)
    result = run_plan_agent(case.request, materials, PlanAgentFixtureModel())
    if result.plan is None:
        raise WeatherRepairEvaluationError("weather fixture requires a Plan draft")
    return case.request, materials, result.plan


def _weather_risk(
    plan: TripPlan,
    *,
    severity: RiskSeverity = RiskSeverity.MEDIUM,
    activity_type: str = "outdoor",
    day_offset: int = 1,
    starts: time = time(13),
    ends: time = time(17),
) -> WeatherRisk:
    service_date = plan.start_date + timedelta(days=day_offset)
    starts_at = datetime.combine(service_date, starts, tzinfo=CN_TIMEZONE)
    ends_at = datetime.combine(service_date, ends, tzinfo=CN_TIMEZONE)
    material = f"{plan.plan_id}|{service_date}|{severity}|{activity_type}|{starts}|{ends}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    return WeatherRisk(
        risk_id=f"weather-repair-{digest}",
        city=plan.destination_city,
        starts_at=starts_at,
        ends_at=ends_at,
        risk_type=WeatherRiskType.RAIN,
        severity=severity,
        threshold_description="fixture 显著降雨风险。",
        affected_activity_types=(activity_type,),
        advisory="系统主动建议调整受影响的户外活动。",
        source=SourceReference(
            provider="weather-repair-eval-fixture",
            provider_id=f"weather-source-{digest}",
            data_mode=DataMode.FIXTURE,
            retrieved_at=datetime(2026, 9, 21, tzinfo=UTC),
        ),
    )


def _risks_for(case: WeatherRepairEvalCase, plan: TripPlan) -> tuple[WeatherRisk, ...]:
    scenario = case.scenario
    if scenario == WeatherRepairScenario.NO_RISK:
        return ()
    if scenario == WeatherRepairScenario.LOW_SEVERITY:
        return (_weather_risk(plan, severity=RiskSeverity.LOW),)
    if scenario == WeatherRepairScenario.INDOOR_ONLY:
        return (_weather_risk(plan, day_offset=0, starts=time(8), ends=time(12)),)
    if scenario == WeatherRepairScenario.OUTSIDE_TIME:
        return (_weather_risk(plan, starts=time(18), ends=time(20)),)
    if scenario == WeatherRepairScenario.ACTIVITY_TYPE_MISMATCH:
        return (_weather_risk(plan, activity_type="indoor"),)
    return (_weather_risk(plan),)


def _opening_hours(request_id: str, plan: TripPlan) -> OpeningHoursEvidenceBundle:
    candidate_ids = tuple(
        dict.fromkeys(
            item.candidate_id
            for day in plan.days
            for item in day.items
            if item.kind in {ActivityKind.ATTRACTION, ActivityKind.MEAL}
            and item.candidate_id is not None
        )
    )
    items = tuple(
        OpeningHoursEvidence(
            evidence_id=f"weather-opening-{candidate_id}-{service_date.isoformat()}",
            candidate_id=candidate_id,
            service_date=service_date,
            opens_at=datetime.combine(service_date, time(8), tzinfo=CN_TIMEZONE),
            closes_at=datetime.combine(service_date, time(21), tzinfo=CN_TIMEZONE),
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


class WeatherRepairFixtureExecutor:
    def __init__(self, scenario: WeatherRepairScenario) -> None:
        self._scenario = scenario

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
        if self._scenario == WeatherRepairScenario.PERSISTENT_FAILURE:
            return WeatherReplanExecutionResult(
                status=WeatherReplanExecutionStatus.FAILED,
                error_code="weather-fixture-executor-failed",
            )
        if self._scenario == WeatherRepairScenario.IMPACT_REMAINS:
            return WeatherReplanExecutionResult(
                status=WeatherReplanExecutionStatus.SUCCEEDED,
                proposed_plan=plan,
                model_call_count=1,
            )
        impacted_ids = {item.item_id for item in impacts}
        if self._scenario == WeatherRepairScenario.MAJOR_CROSS_DAY:
            proposal = self._cross_day_plan(plan, materials, impacted_ids)
        else:
            proposal = self._shifted_plan(
                plan,
                impacted_ids,
                change_protected_date=self._scenario == WeatherRepairScenario.SCOPE_VIOLATION,
            )
        return WeatherReplanExecutionResult(
            status=WeatherReplanExecutionStatus.SUCCEEDED,
            proposed_plan=proposal,
            model_call_count=1,
        )

    @staticmethod
    def _shifted_plan(
        plan: TripPlan,
        impacted_ids: set[str],
        *,
        change_protected_date: bool,
    ) -> TripPlan:
        days: list[DayPlan] = []
        for day in plan.days:
            items = tuple(
                _shift_item(item, timedelta(hours=3, minutes=30))
                if item.item_id in impacted_ids
                else item
                for item in day.items
            )
            if change_protected_date and day.date == plan.start_date:
                items = tuple(
                    _shift_item(item, timedelta(minutes=15)) if index == 0 else item
                    for index, item in enumerate(items)
                )
            days.append(
                day.model_copy(
                    update={"items": tuple(sorted(items, key=lambda item: item.start_at))}
                )
            )
        return _replace_days(plan, tuple(days))

    @staticmethod
    def _cross_day_plan(
        plan: TripPlan,
        materials: PlanningMaterialBundle,
        impacted_ids: set[str],
    ) -> TripPlan:
        source_day = next(
            day for day in plan.days if any(item.item_id in impacted_ids for item in day.items)
        )
        impacted = next(item for item in source_day.items if item.item_id in impacted_ids)
        target_date = source_day.date + timedelta(days=1)
        stay = materials.shortlist.primary_stay
        if stay is None or impacted.candidate_id is None:
            raise WeatherRepairEvaluationError("cross-day fixture requires grounded stay and POI")
        route = next(
            edge.route
            for edge in materials.route_matrix.edges
            if edge.origin_candidate_id == stay.candidate_id
            and edge.destination_candidate_id == impacted.candidate_id
            and edge.status == RouteEdgeStatus.SUCCEEDED
        )
        if route is None:
            raise WeatherRepairEvaluationError("cross-day fixture requires a stay-to-POI route")
        moved = impacted.model_copy(
            update={
                "start_at": datetime.combine(target_date, time(14), tzinfo=CN_TIMEZONE),
                "end_at": datetime.combine(target_date, time(16), tzinfo=CN_TIMEZONE),
                "route_from_previous": route,
            }
        )
        days: list[DayPlan] = []
        for day in plan.days:
            items = tuple(item for item in day.items if item.item_id != impacted.item_id)
            if day.date == target_date:
                items = tuple(sorted((*items, moved), key=lambda item: item.start_at))
            days.append(day.model_copy(update={"items": items}))
        return _replace_days(plan, tuple(days))


def _stable_error_code(error: Exception) -> str:
    material = f"{error.__class__.__module__}|{error.__class__.__qualname__}"
    return f"weather-repair-error-{hashlib.sha256(material.encode()).hexdigest()[:12]}"


def _failed_result(
    case: WeatherRepairEvalCase,
    error: Exception,
) -> WeatherRepairCaseResult:
    return WeatherRepairCaseResult(
        case_id=case.case_id,
        passed=False,
        expected_outcome=case.expected.outcome,
        actual_outcome=case.expected.outcome,
        expected_stop_reason=case.expected.stop_reason,
        actual_stop_reason=case.expected.stop_reason,
        expected_impact_count=case.expected.impact_count,
        actual_impact_count=0,
        expected_attempt_count=case.expected.attempt_count,
        actual_attempt_count=0,
        expected_change_grade=case.expected.change_grade,
        actual_change_grade=WeatherChangeGrade.NONE,
        expected_confirmation=case.expected.requires_user_confirmation,
        actual_confirmation=False,
        task_created_proactively=False,
        source_traceable_impact_count=0,
        retry_bound_respected=False,
        effective_plan_preserved_for_hitl_or_failure=False,
        deterministic_replay=False,
        delegated_model_call_count=0,
        error_code=_stable_error_code(error),
        checks=(EvaluationCheck(code="workflow_completed", passed=False),),
    )


async def evaluate_weather_repair_case(
    case: WeatherRepairEvalCase,
) -> WeatherRepairCaseResult:
    try:
        request, materials, plan = await _base_fixture()
        risks = _risks_for(case, plan)
        opening_hours = _opening_hours(request.request_id, plan)
        first = await run_weather_repair(
            request,
            plan,
            materials,
            opening_hours,
            risks,
            WeatherRepairFixtureExecutor(case.scenario),
        )
        second = await run_weather_repair(
            request,
            plan,
            materials,
            opening_hours,
            risks,
            WeatherRepairFixtureExecutor(case.scenario),
        )
    except Exception as error:
        return _failed_result(case, error)

    source_traceable = sum(
        impact.risk_source.provider_id is not None
        and impact.risk_source.data_mode == DataMode.FIXTURE
        for impact in first.impacts
    )
    task_created = first.task is not None and not any(
        marker in request.raw_text for marker in ("天气", "下雨", "降雨")
    )
    should_preserve = first.outcome in {
        WeatherRepairOutcome.WAITING_FOR_USER,
        WeatherRepairOutcome.UNRESOLVED,
    }
    effective_preserved = not should_preserve or first.effective_plan == plan
    checks = (
        EvaluationCheck(code="workflow_completed", passed=True),
        EvaluationCheck(code="outcome_matches", passed=first.outcome == case.expected.outcome),
        EvaluationCheck(
            code="stop_reason_matches",
            passed=first.stop_reason == case.expected.stop_reason,
        ),
        EvaluationCheck(
            code="impact_count_matches",
            passed=len(first.impacts) == case.expected.impact_count,
        ),
        EvaluationCheck(
            code="attempt_count_matches",
            passed=len(first.attempts) == case.expected.attempt_count,
        ),
        EvaluationCheck(
            code="change_grade_matches",
            passed=first.change.grade == case.expected.change_grade,
        ),
        EvaluationCheck(
            code="confirmation_matches",
            passed=(first.requires_user_confirmation == case.expected.requires_user_confirmation),
        ),
        EvaluationCheck(
            code="proactive_task_routing",
            passed=task_created == (case.expected.impact_count > 0),
        ),
        EvaluationCheck(
            code="risk_sources_traceable",
            passed=source_traceable == len(first.impacts),
        ),
        EvaluationCheck(
            code="retry_bound_respected",
            passed=len(first.attempts) <= 2,
        ),
        EvaluationCheck(
            code="effective_plan_safety",
            passed=effective_preserved,
        ),
        EvaluationCheck(code="deterministic_replay", passed=first == second),
    )
    return WeatherRepairCaseResult(
        case_id=case.case_id,
        passed=all(item.passed for item in checks),
        expected_outcome=case.expected.outcome,
        actual_outcome=first.outcome,
        expected_stop_reason=case.expected.stop_reason,
        actual_stop_reason=first.stop_reason,
        expected_impact_count=case.expected.impact_count,
        actual_impact_count=len(first.impacts),
        expected_attempt_count=case.expected.attempt_count,
        actual_attempt_count=len(first.attempts),
        expected_change_grade=case.expected.change_grade,
        actual_change_grade=first.change.grade,
        expected_confirmation=case.expected.requires_user_confirmation,
        actual_confirmation=first.requires_user_confirmation,
        task_created_proactively=task_created,
        source_traceable_impact_count=source_traceable,
        retry_bound_respected=len(first.attempts) <= 2,
        effective_plan_preserved_for_hitl_or_failure=effective_preserved,
        deterministic_replay=first == second,
        delegated_model_call_count=first.total_model_call_count,
        checks=checks,
    )


async def evaluate_weather_repair_suite(
    suite_path: Path = WEATHER_REPAIR_SUITE_PATH,
) -> WeatherRepairBaselineReport:
    suite = load_weather_repair_suite(suite_path)
    results = tuple([await evaluate_weather_repair_case(case) for case in suite.cases])
    passed = sum(item.passed for item in results)
    total_impacts = sum(item.actual_impact_count for item in results)
    traceable_impacts = sum(item.source_traceable_impact_count for item in results)
    return WeatherRepairBaselineReport(
        dataset_sha256=weather_repair_dataset_sha256(suite),
        passed_case_count=passed,
        case_pass_rate=expected_rate(passed, len(results)),
        no_false_positive_case_count=sum(
            item.actual_outcome == WeatherRepairOutcome.NO_ACTION for item in results
        ),
        proactive_task_case_count=sum(item.task_created_proactively for item in results),
        auto_applied_case_count=sum(
            item.actual_outcome == WeatherRepairOutcome.AUTO_APPLIED for item in results
        ),
        hitl_case_count=sum(
            item.actual_outcome == WeatherRepairOutcome.WAITING_FOR_USER for item in results
        ),
        bounded_retry_case_count=sum(item.actual_attempt_count == 2 for item in results),
        source_traceability_rate=expected_rate(traceable_impacts, total_impacts),
        deterministic_replay_case_count=sum(item.deterministic_replay for item in results),
        delegated_model_call_count=sum(item.delegated_model_call_count for item in results),
        results=results,
        limitations=(
            "天气风险和重规划执行器来自版本化 fixture, 不代表实时预报准确率或高德可用性。",
            "Coordinator 自身零模型调用; fixture executor 隔离验证触发、分级和安全边界。",
            "minor 自动采用仍要求作用域保护、风险消除与 Hard Validator 全部通过。",
            (
                "major 仅生成 pending_confirmation 方案; "
                "API 级暂停、恢复与用户操作将在任务图阶段接线。"
            ),
            "定时 WeatherWatch 刷新频率属于后续任务, 本阶段入口接收最新 Provider 风险快照。",
        ),
    )
