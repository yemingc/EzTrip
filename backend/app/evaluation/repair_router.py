import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.agents.plan_agent import run_plan_agent
from app.domain.money import BudgetCategory, CostItem, MoneyRange
from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import ActivityKind, TripPlan
from app.domain.request import BudgetConstraint, TripRequest
from app.domain.sources import DataMode, SourceReference
from app.domain.validation import IssueSeverity, RepairAction, ResponsibleNode, ValidationIssue
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.hard_validator import (
    _mutate_materials,
    _mutate_plan,
    _mutate_request,
    _opening_hours,
    _source_cases,
    load_hard_validator_suite,
)
from app.evaluation.hard_validator_contracts import (
    HardPlanMutation,
    HardValidatorEvalCase,
    OpeningEvidenceScenario,
)
from app.evaluation.plan_agent import PlanAgentFixtureModel, build_plan_agent_materials
from app.evaluation.repair_router_contracts import (
    RepairExecutorScenario,
    RepairFixtureSetup,
    RepairRouterBaselineReport,
    RepairRouterCaseResult,
    RepairRouterEvalCase,
    RepairRouterEvalSuite,
)
from app.planning.material_contracts import PlanningMaterialBundle
from app.planning.repair_contracts import RepairExecutionResult, RepairExecutionStatus
from app.planning.repair_router import PIPELINE_NODES, run_repair_router

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
REPAIR_ROUTER_SUITE_PATH = REPOSITORY_ROOT / "evals" / "cases" / "repair-router" / "suite.v1.json"


class RepairRouterEvaluationError(RuntimeError):
    """Raised when the Repair Router fixture inventory contradicts its references."""


@dataclass(frozen=True)
class _RepairFixture:
    request: TripRequest
    initial_materials: PlanningMaterialBundle
    initial_plan: TripPlan
    initial_opening: OpeningHoursEvidenceBundle
    repaired_materials: PlanningMaterialBundle
    repaired_plan: TripPlan
    repaired_opening: OpeningHoursEvidenceBundle


class RepairRouterFixtureExecutor:
    def __init__(self, case: RepairRouterEvalCase, fixture: _RepairFixture) -> None:
        self._case = case
        self._fixture = fixture
        self.call_count = 0

    async def repair(
        self,
        request: TripRequest,
        plan: TripPlan,
        materials: PlanningMaterialBundle,
        opening_hours: OpeningHoursEvidenceBundle,
        issues: tuple[ValidationIssue, ...],
        repair_action: RepairAction,
        action_attempt: int,
    ) -> RepairExecutionResult:
        del request, issues
        self.call_count += 1
        try:
            executed_nodes = self._case.expected.executed_nodes_by_attempt[self.call_count - 1]
        except IndexError as error:
            raise RepairRouterEvaluationError("unexpected fixture repair call") from error

        if self._case.executor_scenario == RepairExecutorScenario.UNUSED:
            raise RepairRouterEvaluationError("unused fixture executor was invoked")
        model_calls = int(
            any(
                node in {ResponsibleNode.EXPLORE, ResponsibleNode.STAY, ResponsibleNode.PLAN}
                for node in executed_nodes
            )
        )
        provider_calls = int(
            any(
                node
                in {
                    ResponsibleNode.EXPLORE,
                    ResponsibleNode.STAY,
                    ResponsibleNode.ROUTE,
                    ResponsibleNode.BUDGET,
                }
                for node in executed_nodes
            )
        )
        if self._case.executor_scenario == RepairExecutorScenario.PERSISTENT_FAILURE:
            return RepairExecutionResult(
                status=RepairExecutionStatus.FAILED,
                materials=materials,
                plan=plan,
                opening_hours=opening_hours,
                executed_nodes=executed_nodes,
                model_call_count=model_calls,
                provider_call_count=provider_calls,
                error_code=f"fixture-{repair_action.value}-unavailable",
            )
        if action_attempt != 1:
            raise RepairRouterEvaluationError("successful fixture repair should finish once")
        return RepairExecutionResult(
            status=RepairExecutionStatus.SUCCEEDED,
            materials=self._fixture.repaired_materials,
            plan=self._fixture.repaired_plan,
            opening_hours=self._fixture.repaired_opening,
            executed_nodes=executed_nodes,
            model_call_count=model_calls,
            provider_call_count=provider_calls,
        )


def load_repair_router_suite(
    suite_path: Path = REPAIR_ROUTER_SUITE_PATH,
) -> RepairRouterEvalSuite:
    return RepairRouterEvalSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))


def _hard_cases() -> dict[str, HardValidatorEvalCase]:
    return {item.case_id: item for item in load_hard_validator_suite().cases}


def repair_router_dataset_sha256(suite: RepairRouterEvalSuite) -> str:
    hard_cases = _hard_cases()
    references: list[dict[str, object]] = []
    for case in suite.cases:
        try:
            hard_case = hard_cases[case.source_hard_validator_case_id]
        except KeyError as error:
            raise RepairRouterEvaluationError(
                f"unknown hard-validator case reference: {error.args[0]}"
            ) from error
        references.append(hard_case.model_dump(mode="json"))
    canonical = json.dumps(
        {
            "suite": suite.model_dump(mode="json"),
            "referenced_hard_validator_cases": references,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _repair_tight_transfer(plan: TripPlan) -> TripPlan:
    payload = plan.model_dump(mode="python")
    days = payload["days"]
    assert isinstance(days, (list, tuple))
    changed = False
    for day in days:
        assert isinstance(day, dict)
        items = day["items"]
        assert isinstance(items, (list, tuple))
        previous: dict[str, object] | None = None
        for item in items:
            assert isinstance(item, dict)
            if item.get("kind") not in {ActivityKind.ATTRACTION, ActivityKind.MEAL}:
                continue
            if previous is not None:
                route = item.get("route_from_previous")
                assert isinstance(route, dict)
                previous_end = previous["end_at"]
                start_at = item["start_at"]
                end_at = item["end_at"]
                assert isinstance(previous_end, datetime)
                assert isinstance(start_at, datetime)
                assert isinstance(end_at, datetime)
                required_minutes = route["duration_minutes"]
                assert isinstance(required_minutes, int)
                earliest_start = previous_end + timedelta(minutes=required_minutes)
                if start_at < earliest_start:
                    previous_start = previous["start_at"]
                    assert isinstance(previous_start, datetime)
                    latest_previous_end = start_at - timedelta(minutes=required_minutes)
                    if latest_previous_end > previous_start:
                        previous["end_at"] = latest_previous_end
                    else:
                        duration = end_at - start_at
                        item["start_at"] = earliest_start
                        item["end_at"] = earliest_start + duration
                    changed = True
            previous = item
    if not changed:
        raise RepairRouterEvaluationError("tight-transfer fixture did not contain a conflict")
    return TripPlan.model_validate(payload)


def _apply_budget_floor(request: TripRequest, plan: TripPlan) -> tuple[TripRequest, TripPlan]:
    request_payload = request.model_dump(mode="python")
    request_payload["budget"] = BudgetConstraint(
        total_limit=Decimal("100.00"),
        included_categories=(BudgetCategory.ADMISSION,),
        hard_limit=True,
    )
    budget_request = TripRequest.model_validate(request_payload)
    cost_item = CostItem(
        cost_item_id="repair-budget-floor-cost",
        category=BudgetCategory.ADMISSION,
        description="fixture 可追溯门票费用",
        quantity=Decimal("1"),
        unit_price=MoneyRange(minimum=Decimal("150.00"), maximum=Decimal("150.00")),
        source=SourceReference(
            provider="repair-router-budget-fixture",
            provider_id="repair-budget-floor-source",
            data_mode=DataMode.FIXTURE,
            retrieved_at=datetime(2026, 9, 20, tzinfo=UTC),
        ),
        is_estimate=False,
    )
    plan_payload = plan.model_dump(mode="python")
    plan_payload["cost_items"] = (cost_item,)
    return budget_request, TripPlan.model_validate(plan_payload)


async def _build_fixture(case: RepairRouterEvalCase) -> _RepairFixture:
    try:
        hard_case = _hard_cases()[case.source_hard_validator_case_id]
        source_case = _source_cases()[hard_case.source_plan_case_id]
    except KeyError as error:
        raise RepairRouterEvaluationError(
            f"unknown repair fixture reference: {error.args[0]}"
        ) from error
    base_materials = await build_plan_agent_materials(source_case)
    request = _mutate_request(source_case.request, base_materials, hard_case.constraint_scenario)
    if request != source_case.request:
        source_case = source_case.model_copy(update={"request": request})
        base_materials = await build_plan_agent_materials(source_case)
    plan_result = run_plan_agent(request, base_materials, PlanAgentFixtureModel())
    if plan_result.plan is None:
        raise RepairRouterEvaluationError("repair fixture requires a Plan draft")
    base_plan = plan_result.plan
    initial_materials = _mutate_materials(base_materials, hard_case.material_mutation)
    initial_plan = _mutate_plan(base_plan, hard_case.plan_mutation)
    initial_opening = _opening_hours(request, initial_plan, hard_case.opening_evidence)
    repaired_plan = base_plan
    if hard_case.plan_mutation == HardPlanMutation.TIGHT_TRANSFER:
        repaired_plan = _repair_tight_transfer(initial_plan)
    repaired_opening = _opening_hours(
        request,
        repaired_plan,
        OpeningEvidenceScenario.COMPLETE,
    )
    if hard_case.plan_mutation == HardPlanMutation.TIGHT_TRANSFER:
        repaired_opening = initial_opening
    if case.setup == RepairFixtureSetup.BUDGET_FLOOR:
        request, initial_plan = _apply_budget_floor(request, initial_plan)
        repaired_plan = initial_plan
        initial_opening = _opening_hours(
            request,
            initial_plan,
            OpeningEvidenceScenario.COMPLETE,
        )
        repaired_opening = initial_opening
    return _RepairFixture(
        request=request,
        initial_materials=initial_materials,
        initial_plan=initial_plan,
        initial_opening=initial_opening,
        repaired_materials=base_materials,
        repaired_plan=repaired_plan,
        repaired_opening=repaired_opening,
    )


def _stable_error_code(error: Exception) -> str:
    material = f"{error.__class__.__module__}|{error.__class__.__qualname__}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"repair-router-error-{digest}"


def _failed_result(case: RepairRouterEvalCase, error: Exception) -> RepairRouterCaseResult:
    return RepairRouterCaseResult(
        case_id=case.case_id,
        passed=False,
        expected_outcome=case.expected.outcome,
        actual_outcome=case.expected.outcome,
        expected_stop_reason=case.expected.stop_reason,
        actual_stop_reason=case.expected.stop_reason,
        expected_attempt_actions=case.expected.attempt_actions,
        actual_attempt_actions=(),
        expected_executed_nodes=case.expected.executed_nodes_by_attempt,
        actual_executed_nodes=(),
        initial_error_codes=(),
        expected_pending_error_codes=case.expected.pending_error_codes,
        actual_pending_error_codes=(),
        retry_bound_respected=False,
        unaffected_nodes_reused=False,
        deterministic_replay=False,
        delegated_model_call_count=0,
        error_code=_stable_error_code(error),
        checks=(EvaluationCheck(code="workflow_completed", passed=False),),
    )


async def evaluate_repair_router_case(case: RepairRouterEvalCase) -> RepairRouterCaseResult:
    try:
        fixture = await _build_fixture(case)
        first = await run_repair_router(
            fixture.request,
            fixture.initial_plan,
            fixture.initial_materials,
            fixture.initial_opening,
            RepairRouterFixtureExecutor(case, fixture),
        )
        second = await run_repair_router(
            fixture.request,
            fixture.initial_plan,
            fixture.initial_materials,
            fixture.initial_opening,
            RepairRouterFixtureExecutor(case, fixture),
        )
    except Exception as error:
        return _failed_result(case, error)

    actual_actions = tuple(item.repair_action for item in first.attempts)
    actual_nodes = tuple(item.executed_nodes for item in first.attempts)
    initial_errors = tuple(
        item.rule_code
        for item in first.initial_report.issues
        if item.severity == IssueSeverity.ERROR
    )
    retry_bound_respected = all(item.attempt_count <= 2 for item in first.retry_counts)
    unaffected_nodes_reused = all(
        set(attempt.reused_nodes) == set(PIPELINE_NODES) - set(attempt.executed_nodes)
        for attempt in first.attempts
    )
    checks = (
        EvaluationCheck(code="workflow_completed", passed=True),
        EvaluationCheck(code="router_zero_model_calls", passed=True),
        EvaluationCheck(code="outcome_matches", passed=first.outcome == case.expected.outcome),
        EvaluationCheck(
            code="stop_reason_matches",
            passed=first.stop_reason == case.expected.stop_reason,
        ),
        EvaluationCheck(
            code="finalization_matches",
            passed=first.final_report.can_finalize == case.expected.final_can_finalize,
        ),
        EvaluationCheck(
            code="targeted_actions_match",
            passed=actual_actions == case.expected.attempt_actions,
        ),
        EvaluationCheck(
            code="executed_nodes_match",
            passed=actual_nodes == case.expected.executed_nodes_by_attempt,
        ),
        EvaluationCheck(
            code="pending_errors_match",
            passed=first.pending_error_codes == case.expected.pending_error_codes,
        ),
        EvaluationCheck(code="retry_bound_respected", passed=retry_bound_respected),
        EvaluationCheck(code="unaffected_nodes_reused", passed=unaffected_nodes_reused),
        EvaluationCheck(code="deterministic_replay", passed=first == second),
    )
    return RepairRouterCaseResult(
        case_id=case.case_id,
        passed=all(item.passed for item in checks),
        expected_outcome=case.expected.outcome,
        actual_outcome=first.outcome,
        expected_stop_reason=case.expected.stop_reason,
        actual_stop_reason=first.stop_reason,
        expected_attempt_actions=case.expected.attempt_actions,
        actual_attempt_actions=actual_actions,
        expected_executed_nodes=case.expected.executed_nodes_by_attempt,
        actual_executed_nodes=actual_nodes,
        initial_error_codes=initial_errors,
        expected_pending_error_codes=case.expected.pending_error_codes,
        actual_pending_error_codes=first.pending_error_codes,
        retry_bound_respected=retry_bound_respected,
        unaffected_nodes_reused=unaffected_nodes_reused,
        deterministic_replay=first == second,
        delegated_model_call_count=first.total_model_call_count,
        checks=checks,
    )


async def evaluate_repair_router_suite(
    suite_path: Path = REPAIR_ROUTER_SUITE_PATH,
) -> RepairRouterBaselineReport:
    suite = load_repair_router_suite(suite_path)
    results = tuple([await evaluate_repair_router_case(case) for case in suite.cases])
    passed_case_count = sum(item.passed for item in results)
    exact_route_case_count = sum(
        item.actual_attempt_actions == item.expected_attempt_actions
        and item.actual_executed_nodes == item.expected_executed_nodes
        for item in results
    )
    return RepairRouterBaselineReport(
        dataset_sha256=repair_router_dataset_sha256(suite),
        passed_case_count=passed_case_count,
        case_pass_rate=expected_rate(passed_case_count, len(results)),
        exact_route_case_count=exact_route_case_count,
        exact_route_rate=expected_rate(exact_route_case_count, len(results)),
        retry_bound_case_count=sum(item.retry_bound_respected for item in results),
        unaffected_reuse_case_count=sum(item.unaffected_nodes_reused for item in results),
        deterministic_replay_case_count=sum(item.deterministic_replay for item in results),
        total_repair_attempt_count=sum(len(item.actual_attempt_actions) for item in results),
        delegated_model_call_count=sum(item.delegated_model_call_count for item in results),
        results=results,
        limitations=(
            "Repair Router 自身是零模型调用的确定性编排器; fixture executor 只模拟责任节点产物。",
            "本报告验证路由、重试、停止条件、产物复用和 trace, 不代表实时高德或价格来源可用性。",
            (
                "成功案例恢复版本化 fixture 事实; 真实 Explore、Stay、Route 与 Plan "
                "重跑将在任务 API 图中接线。"
            ),
            "预算费用下界超过硬限制时只进入 HITL, 不自动提高预算或删除用户约束。",
            "warning 不阻止定稿, 也不会触发 Router 自动循环; Review/UI 后续负责向用户展示。",
        ),
    )
