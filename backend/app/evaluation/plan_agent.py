import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from app.agents.contracts import (
    ModelTokenUsage,
    PlannerModelResponse,
    PlannerPlacementProposal,
    PlannerProposalBatch,
)
from app.agents.plan_agent_contracts import PlanAgentRunResult, PlanAgentRunStatus
from app.domain.candidates import ActivityEnvironment, CandidatePOI
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import RiskSeverity, WeatherRisk, WeatherRiskType
from app.domain.validation import PlanValidationStatus
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.explore import load_explore_agent_suite, materialize_fixture_candidate
from app.evaluation.plan_agent_contracts import (
    PlanAgentBaselineReport,
    PlanAgentCaseResult,
    PlanAgentEvalCase,
    PlanAgentEvalSuite,
)
from app.evaluation.planning_materials import (
    PlanningMaterialFixtureExploreModel,
    PlanningMaterialFixtureStayModel,
    PlanningMaterialRouteProvider,
)
from app.evaluation.specialist_fanout import SpecialistScenarioProvider
from app.evaluation.stay import load_stay_agent_suite, materialize_stay_fixture_candidate
from app.planning.material_builder import build_planning_material_bundle
from app.planning.material_contracts import PlanningMaterialBundle, RouteEdgeStatus
from app.planning.specialist_fanout import run_specialist_fanout

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PLAN_AGENT_SUITE_PATH = REPOSITORY_ROOT / "evals" / "cases" / "plan-agent" / "suite.v1.json"

PlanAgentRunner = Callable[[PlanAgentEvalCase, PlanningMaterialBundle], PlanAgentRunResult]


class PlanAgentEvaluationError(RuntimeError):
    """Raised when a Plan Agent fixture contradicts its versioned references."""


class PlanAgentFixtureModel:
    """Route/weather-aware fixed model used to isolate the deterministic Plan Agent path."""

    def propose(self, materials: PlanningMaterialBundle) -> PlannerModelResponse:
        weather_dates = {
            risk.starts_at.date() + timedelta(days=offset)
            for branch in materials.specialist_result.branches
            for risk in branch.weather_risks
            for offset in range((risk.ends_at.date() - risk.starts_at.date()).days + 1)
        }
        days = tuple(item.date for item in materials.planner_context.days)
        remaining_slots = {day: ["09:00", "14:00"] for day in days}
        proposals: list[PlannerPlacementProposal] = []
        for candidate in materials.shortlist.poi_candidates:
            preferred_days = _preferred_days(candidate, days, weather_dates)
            selected_day = next(day for day in preferred_days if remaining_slots[day])
            start_time = remaining_slots[selected_day].pop(0)
            proposals.append(
                PlannerPlacementProposal(
                    candidate_id=candidate.candidate_id,
                    day_number=days.index(selected_day) + 1,
                    start_time=start_time,
                    reason="根据逐日天气、路线材料和活动环境进行可重放夹具排程。",
                )
            )
        return PlannerModelResponse(
            proposal=PlannerProposalBatch(items=tuple(proposals)),
            model="fixture-plan-agent-model",
            latency_ms=25,
            usage=ModelTokenUsage(
                prompt_tokens=200,
                completion_tokens=40,
                total_tokens=240,
            ),
        )


def _preferred_days(
    candidate: CandidatePOI,
    days: tuple[date, ...],
    weather_dates: set[date],
) -> tuple[date, ...]:
    rainy = tuple(day for day in days if day in weather_dates)
    dry = tuple(day for day in days if day not in weather_dates)
    if candidate.environment == ActivityEnvironment.INDOOR:
        return (*rainy, *dry)
    return (*dry, *rainy)


def load_plan_agent_suite(
    suite_path: Path = PLAN_AGENT_SUITE_PATH,
) -> PlanAgentEvalSuite:
    return PlanAgentEvalSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))


def _referenced_fixture_payload(suite: PlanAgentEvalSuite) -> tuple[dict[str, object], ...]:
    explore_by_id = {item.case_id: item for item in load_explore_agent_suite().cases}
    stay_by_id = {item.case_id: item for item in load_stay_agent_suite().cases}
    payload: list[dict[str, object]] = []
    for case in suite.cases:
        try:
            explore = explore_by_id[case.explore_fixture_case_id]
            stay = stay_by_id[case.stay_fixture_case_id]
        except KeyError as error:
            raise PlanAgentEvaluationError(
                f"unknown Plan Agent fixture reference: {error.args[0]}"
            ) from error
        payload.append(
            {
                "case_id": case.case_id,
                "explore": explore.model_dump(mode="json"),
                "stay": stay.model_dump(mode="json"),
            }
        )
    return tuple(payload)


def plan_agent_dataset_sha256(suite: PlanAgentEvalSuite) -> str:
    canonical = json.dumps(
        {
            "suite": suite.model_dump(mode="json"),
            "referenced_fixtures": _referenced_fixture_payload(suite),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _weather_risk(case: PlanAgentEvalCase) -> WeatherRisk:
    starts_at = datetime.combine(case.request.start_date, time(8), tzinfo=UTC)
    material = f"{case.case_id}|rain|fixture"
    return WeatherRisk(
        risk_id=f"weather-{case.case_id}",
        city=case.request.destination_city,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=12),
        risk_type=WeatherRiskType.RAIN,
        severity=RiskSeverity.MEDIUM,
        threshold_description="fixture 预报包含中雨。",
        affected_activity_types=("outdoor",),
        advisory="主动提示减少长时间户外活动。",
        source=SourceReference(
            provider="plan-agent-weather-fixture",
            provider_id=f"weather-{case.case_id}",
            data_mode=DataMode.FIXTURE,
            retrieved_at=datetime(2026, 8, 21, tzinfo=UTC),
            raw_response_sha256=hashlib.sha256(material.encode("utf-8")).hexdigest(),
        ),
    )


async def build_plan_agent_materials(case: PlanAgentEvalCase) -> PlanningMaterialBundle:
    explore_by_id = {item.case_id: item for item in load_explore_agent_suite().cases}
    stay_by_id = {item.case_id: item for item in load_stay_agent_suite().cases}
    try:
        explore = explore_by_id[case.explore_fixture_case_id]
        stay = stay_by_id[case.stay_fixture_case_id]
    except KeyError as error:
        raise PlanAgentEvaluationError(
            f"unknown Plan Agent fixture reference: {error.args[0]}"
        ) from error
    provider = SpecialistScenarioProvider(
        tuple(materialize_fixture_candidate(item) for item in explore.provider_candidates),
        tuple(materialize_stay_fixture_candidate(item) for item in stay.provider_candidates),
        (_weather_risk(case),),
        failure=None,
        require_parallel_entry=False,
    )
    specialist_result = await run_specialist_fanout(
        case.request,
        provider,
        PlanningMaterialFixtureExploreModel(),
        PlanningMaterialFixtureStayModel(),
        data_mode=DataMode.FIXTURE,
    )
    return await build_planning_material_bundle(
        specialist_result,
        PlanningMaterialRouteProvider(case.route_failure),
    )


def _stable_error_code(error: Exception) -> str:
    material = f"{error.__class__.__module__}|{error.__class__.__qualname__}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"plan-agent-error-{digest}"


def _failed_result(case: PlanAgentEvalCase, error: Exception) -> PlanAgentCaseResult:
    return PlanAgentCaseResult(
        case_id=case.case_id,
        passed=False,
        expected_material_status=case.expected.material_status,
        expected_run_status=case.expected.run_status,
        candidate_count=0,
        scheduled_candidate_count=0,
        grounded_candidate_count=0,
        traceable_candidate_count=0,
        route_backed_candidate_count=0,
        input_weather_risk_count=0,
        preserved_weather_risk_count=0,
        day_count=0,
        cost_item_count=0,
        model_call_count=0,
        latency_ms=0,
        error_code=_stable_error_code(error),
        checks=(EvaluationCheck(code="workflow_completed", passed=False),),
    )


async def evaluate_plan_agent_case(
    case: PlanAgentEvalCase,
    runner: PlanAgentRunner,
) -> PlanAgentCaseResult:
    try:
        materials = await build_plan_agent_materials(case)
        result = runner(case, materials)
    except Exception as error:
        return _failed_result(case, error)

    candidates_by_id = {item.candidate_id: item for item in materials.shortlist.poi_candidates}
    decisions = result.decisions
    scheduled = tuple(item.item for item in decisions)
    grounded = tuple(
        item
        for item in scheduled
        if item.candidate_id in candidates_by_id
        and item.title == candidates_by_id[item.candidate_id].name
        and item.source == candidates_by_id[item.candidate_id].source
    )
    traceable = tuple(
        item
        for item in grounded
        if item.source is not None
        and item.source.provider_id is not None
        and item.source.data_mode == DataMode.FIXTURE
    )
    edges_by_id = {item.edge_id: item for item in materials.route_matrix.edges}
    route_backed = tuple(
        decision
        for decision in decisions
        if decision.route_edge_id in edges_by_id
        and edges_by_id[decision.route_edge_id].status == RouteEdgeStatus.SUCCEEDED
        and edges_by_id[decision.route_edge_id].route == decision.item.route_from_previous
    )
    input_weather_ids = tuple(
        risk.risk_id
        for branch in materials.specialist_result.branches
        for risk in branch.weather_risks
    )
    plan_weather_ids = (
        tuple(item.risk_id for item in result.plan.weather_risks) if result.plan is not None else ()
    )
    full_dates = result.plan is not None and tuple(day.date for day in result.plan.days) == tuple(
        item.date for item in materials.planner_context.days
    )
    checks = (
        EvaluationCheck(code="workflow_completed", passed=True),
        EvaluationCheck(
            code="material_status_matches",
            passed=materials.status == case.expected.material_status,
        ),
        EvaluationCheck(
            code="run_status_matches",
            passed=result.status == case.expected.run_status,
        ),
        EvaluationCheck(
            code="model_call_routing_matches",
            passed=result.model_call_count == case.expected.model_call_count,
        ),
        EvaluationCheck(
            code="candidate_scope_matches",
            passed=len(candidates_by_id) == case.expected.candidate_count,
        ),
        EvaluationCheck(
            code="exact_candidate_coverage",
            passed=(
                len(scheduled) == len(candidates_by_id)
                if result.status == PlanAgentRunStatus.PLANNED
                else not scheduled
            ),
        ),
        EvaluationCheck(
            code="candidate_facts_grounded",
            passed=len(grounded) == len(scheduled),
        ),
        EvaluationCheck(
            code="candidate_sources_traceable",
            passed=len(traceable) == len(grounded),
        ),
        EvaluationCheck(
            code="route_lineage_preserved",
            passed=len(route_backed) == len(scheduled),
        ),
        EvaluationCheck(
            code="weather_output_preserved",
            passed=(
                plan_weather_ids == input_weather_ids
                if result.status == PlanAgentRunStatus.PLANNED
                else result.plan is None
            ),
        ),
        EvaluationCheck(
            code="complete_trip_dates",
            passed=(
                full_dates if result.status == PlanAgentRunStatus.PLANNED else result.plan is None
            ),
        ),
        EvaluationCheck(
            code="budget_targets_not_price_claims",
            passed=result.plan is None or not result.plan.cost_items,
        ),
        EvaluationCheck(
            code="soft_budget_does_not_create_hard_conflict",
            passed=(
                result.validation is not None
                and result.validation.status != PlanValidationStatus.CONFLICTED
                if result.status == PlanAgentRunStatus.PLANNED
                else result.validation is None
            ),
        ),
    )
    return PlanAgentCaseResult(
        case_id=case.case_id,
        passed=all(item.passed for item in checks),
        expected_material_status=case.expected.material_status,
        actual_material_status=materials.status,
        expected_run_status=case.expected.run_status,
        actual_run_status=result.status,
        candidate_count=len(candidates_by_id),
        scheduled_candidate_count=len(scheduled),
        grounded_candidate_count=len(grounded),
        traceable_candidate_count=len(traceable),
        route_backed_candidate_count=len(route_backed),
        input_weather_risk_count=len(input_weather_ids),
        preserved_weather_risk_count=len(set(input_weather_ids) & set(plan_weather_ids)),
        day_count=len(result.plan.days) if result.plan is not None else 0,
        cost_item_count=len(result.plan.cost_items) if result.plan is not None else 0,
        model_call_count=result.model_call_count,
        validation_status=result.validation.status if result.validation is not None else None,
        latency_ms=result.latency_ms,
        usage=result.usage,
        checks=checks,
    )


async def evaluate_plan_agent_suite(
    runner: PlanAgentRunner,
    *,
    execution_mode: Literal["fixture", "live"],
    model: str,
    suite_path: Path = PLAN_AGENT_SUITE_PATH,
) -> PlanAgentBaselineReport:
    suite = load_plan_agent_suite(suite_path)
    results = tuple([await evaluate_plan_agent_case(case, runner) for case in suite.cases])
    planned = tuple(
        item for item in results if item.actual_run_status == PlanAgentRunStatus.PLANNED
    )
    skipped = tuple(
        item for item in results if item.actual_run_status == PlanAgentRunStatus.SKIPPED
    )
    scheduled = sum(item.scheduled_candidate_count for item in planned)
    weather_input = sum(item.input_weather_risk_count for item in planned)
    latencies = sorted(item.latency_ms for item in planned)
    return PlanAgentBaselineReport(
        execution_mode=execution_mode,
        model=model,
        dataset_sha256=plan_agent_dataset_sha256(suite),
        passed_case_count=sum(item.passed for item in results),
        case_pass_rate=expected_rate(sum(item.passed for item in results), len(results)),
        planned_case_count=len(planned),
        skipped_case_count=len(skipped),
        model_call_count=sum(item.model_call_count for item in results),
        candidate_count=sum(item.candidate_count for item in planned),
        scheduled_candidate_count=scheduled,
        grounding_rate=expected_rate(
            sum(item.grounded_candidate_count for item in planned), scheduled
        ),
        source_traceability_rate=expected_rate(
            sum(item.traceable_candidate_count for item in planned), scheduled
        ),
        route_lineage_rate=expected_rate(
            sum(item.route_backed_candidate_count for item in planned), scheduled
        ),
        weather_preservation_rate=expected_rate(
            sum(item.preserved_weather_risk_count for item in planned), weather_input
        ),
        zero_cost_claim_case_count=sum(item.cost_item_count == 0 for item in planned),
        skipped_zero_model_call_case_count=sum(item.model_call_count == 0 for item in skipped),
        usage_case_count=sum(item.usage is not None for item in results),
        total_prompt_tokens=sum(
            item.usage.prompt_tokens for item in results if item.usage is not None
        ),
        total_completion_tokens=sum(
            item.usage.completion_tokens for item in results if item.usage is not None
        ),
        total_tokens=sum(item.usage.total_tokens for item in results if item.usage is not None),
        p50_latency_ms=_nearest_rank(latencies, 50),
        p95_latency_ms=_nearest_rank(latencies, 95),
        results=results,
        limitations=(
            "候选、住宿、天气与路线来自显式 fixture, 不代表实时高德数据质量。",
            "评测验证候选 grounding、材料消费和停止路由, 不等于行程主观质量准确率。",
            "预算 allocation 是目标 envelope; 当前没有价格事实, 因而不生成 CostItem。",
            "本隔离套件不评估营业时间、must/avoid 等定稿规则; "
            "下游 Hard Validator 另行评测, Repair Router 尚未实现。",
            "live 模式只替换 Plan Agent 模型, 上游 specialist 保持 fixture 以隔离变量。",
        ),
    )


def _nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    rank = (percentile * len(values) + 99) // 100
    return values[max(rank - 1, 0)]
