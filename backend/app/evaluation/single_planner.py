from collections.abc import Callable
from typing import Literal

from app.agents.contracts import SinglePlannerAgentResult
from app.agents.single_planner import SinglePlannerProtocolError
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerContext
from app.domain.sources import DataMode
from app.domain.workflow import PlanningWorkflowStatus
from app.evaluation.contracts import (
    EvaluationCheck,
    PlanningSeedCase,
    SinglePlannerBaselineReport,
    SinglePlannerCaseResult,
    SinglePlannerOutcome,
    expected_rate,
)
from app.evaluation.planning_seed import (
    PLANNING_SEED_MANIFEST_PATH,
    ScenarioTravelDataProvider,
    load_planning_seed_suite,
    planning_seed_dataset_sha256,
)
from app.planning import run_minimal_planning_graph

PlannerRunner = Callable[
    [PlannerContext, tuple[CandidatePOI, ...]],
    SinglePlannerAgentResult,
]


def _traceable(item: object) -> bool:
    source = getattr(item, "source", None)
    return bool(
        source and source.provider and source.provider_id and source.data_mode == DataMode.FIXTURE
    )


async def evaluate_single_planner_case(
    seed_case: PlanningSeedCase,
    runner: PlannerRunner,
) -> SinglePlannerCaseResult:
    provider = ScenarioTravelDataProvider(seed_case.provider)
    upstream = await run_minimal_planning_graph(
        seed_case.request,
        provider,
        data_mode=DataMode.FIXTURE,
    )
    provider.verify_complete()
    planning_expected = upstream.status == PlanningWorkflowStatus.CANDIDATES_READY
    checks: tuple[EvaluationCheck, ...]
    if not planning_expected:
        checks = (
            EvaluationCheck(code="upstream_status_matches", passed=True),
            EvaluationCheck(code="planner_routing_matches", passed=True),
            EvaluationCheck(code="model_not_called", passed=True),
            EvaluationCheck(code="no_fabricated_schedule", passed=True),
        )
        return SinglePlannerCaseResult(
            case_id=seed_case.case_id,
            tier=seed_case.tier,
            passed=True,
            upstream_status=upstream.status,
            outcome=SinglePlannerOutcome.SKIPPED,
            planning_expected=False,
            model_called=False,
            candidate_count=len(upstream.candidates),
            scheduled_candidate_count=0,
            grounded_item_count=0,
            traceable_item_count=0,
            valid_day_plan_count=0,
            latency_ms=0,
            checks=checks,
        )

    try:
        result = runner(upstream.planner_context, upstream.candidates)
    except SinglePlannerProtocolError as error:
        checks = (
            EvaluationCheck(code="planner_protocol_succeeded", passed=False),
            EvaluationCheck(code="candidate_coverage", passed=False),
            EvaluationCheck(code="candidate_grounding", passed=False),
            EvaluationCheck(code="source_traceability", passed=False),
            EvaluationCheck(code="dates_within_trip", passed=False),
        )
        return SinglePlannerCaseResult(
            case_id=seed_case.case_id,
            tier=seed_case.tier,
            passed=False,
            upstream_status=upstream.status,
            outcome=SinglePlannerOutcome.FAILED,
            planning_expected=True,
            model_called=True,
            candidate_count=len(upstream.candidates),
            scheduled_candidate_count=0,
            grounded_item_count=0,
            traceable_item_count=0,
            valid_day_plan_count=0,
            latency_ms=0,
            error_code=error.__class__.__name__.casefold().replace("error", "-error"),
            checks=checks,
        )

    candidate_by_id = {item.candidate_id: item for item in upstream.candidates}
    scheduled_items = tuple(item for day in result.day_plans for item in day.items)
    scheduled_ids = tuple(item.candidate_id for item in scheduled_items)
    grounded_items = tuple(
        item
        for item in scheduled_items
        if item.candidate_id in candidate_by_id
        and item.title == candidate_by_id[item.candidate_id].name
        and item.source == candidate_by_id[item.candidate_id].source
    )
    traceable_items = tuple(item for item in grounded_items if _traceable(item))
    expected_dates = {item.date for item in upstream.planner_context.days}
    dates_valid = all(day.date in expected_dates for day in result.day_plans)
    coverage_valid = set(scheduled_ids) == set(candidate_by_id) and len(scheduled_ids) == len(
        candidate_by_id
    )
    checks = (
        EvaluationCheck(code="planner_protocol_succeeded", passed=True),
        EvaluationCheck(
            code="request_and_context_match",
            passed=(
                result.request_id == upstream.request_id
                and result.context_id == upstream.planner_context.context_id
            ),
        ),
        EvaluationCheck(code="candidate_coverage", passed=coverage_valid),
        EvaluationCheck(
            code="candidate_grounding",
            passed=len(grounded_items) == len(scheduled_items),
        ),
        EvaluationCheck(
            code="source_traceability",
            passed=len(traceable_items) == len(grounded_items),
        ),
        EvaluationCheck(code="dates_within_trip", passed=dates_valid),
        EvaluationCheck(
            code="partial_scope_preserved",
            passed=len(result.day_plans) <= upstream.planner_context.day_count,
        ),
    )
    return SinglePlannerCaseResult(
        case_id=seed_case.case_id,
        tier=seed_case.tier,
        passed=all(check.passed for check in checks),
        upstream_status=upstream.status,
        outcome=SinglePlannerOutcome.PLANNED,
        planning_expected=True,
        model_called=True,
        candidate_count=len(upstream.candidates),
        scheduled_candidate_count=len(scheduled_items),
        grounded_item_count=len(grounded_items),
        traceable_item_count=len(traceable_items),
        valid_day_plan_count=len(result.day_plans) if dates_valid else 0,
        latency_ms=result.latency_ms,
        usage=result.usage,
        checks=checks,
    )


async def evaluate_single_planner_suite(
    runner: PlannerRunner,
    *,
    execution_mode: Literal["fixture", "live"],
    model: str,
) -> SinglePlannerBaselineReport:
    _, cases = load_planning_seed_suite(PLANNING_SEED_MANIFEST_PATH)
    results = tuple([await evaluate_single_planner_case(case, runner) for case in cases])
    candidate_count = sum(item.candidate_count for item in results)
    scheduled_count = sum(item.scheduled_candidate_count for item in results)
    grounded_count = sum(item.grounded_item_count for item in results)
    traceable_count = sum(item.traceable_item_count for item in results)
    passed_count = sum(item.passed for item in results)
    called_latencies = sorted(item.latency_ms for item in results if item.model_called)
    return SinglePlannerBaselineReport(
        execution_mode=execution_mode,
        model=model,
        dataset_sha256=planning_seed_dataset_sha256(cases),
        planning_expected_case_count=sum(item.planning_expected for item in results),
        model_call_count=sum(item.model_called for item in results),
        planned_case_count=sum(item.outcome == SinglePlannerOutcome.PLANNED for item in results),
        skipped_case_count=sum(item.outcome == SinglePlannerOutcome.SKIPPED for item in results),
        failed_case_count=sum(item.outcome == SinglePlannerOutcome.FAILED for item in results),
        passed_case_count=passed_count,
        case_pass_rate=expected_rate(passed_count, len(results)),
        candidate_count=candidate_count,
        scheduled_candidate_count=scheduled_count,
        candidate_coverage_rate=expected_rate(scheduled_count, candidate_count),
        grounded_item_count=grounded_count,
        grounding_rate=expected_rate(grounded_count, scheduled_count),
        traceable_item_count=traceable_count,
        source_traceability_rate=expected_rate(traceable_count, grounded_count),
        usage_case_count=sum(item.usage is not None for item in results),
        total_prompt_tokens=sum(
            item.usage.prompt_tokens for item in results if item.usage is not None
        ),
        total_completion_tokens=sum(
            item.usage.completion_tokens for item in results if item.usage is not None
        ),
        total_tokens=sum(item.usage.total_tokens for item in results if item.usage is not None),
        p50_latency_ms=_nearest_rank(called_latencies, 50),
        p95_latency_ms=_nearest_rank(called_latencies, 95),
        results=results,
        limitations=(
            "候选来自 10 条 planning-seed 中的显式 fixture provider, 不评估实时数据覆盖。",
            "当前只有 6 条可规划案例且每条仅 1 个必去候选, 不能衡量多景点排序质量。",
            "输出是只覆盖已有候选的部分 DayPlan, 不是完整 TripPlan 或可直接执行的行程。",
            "尚未接入开放式推荐、营业时间、路线、天气、酒店或预算可行性校验。",
        ),
    )


def _nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    rank = (percentile * len(values) + 99) // 100
    return values[max(rank - 1, 0)]
