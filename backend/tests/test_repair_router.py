import asyncio

import pytest

from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import TripPlan
from app.domain.request import TripRequest
from app.domain.validation import (
    IssueSeverity,
    PlanValidationReport,
    RepairAction,
    ResponsibleNode,
    ValidationEvidence,
    ValidationIssue,
)
from app.evaluation.hard_validator import _mutate_materials
from app.evaluation.hard_validator_contracts import HardMaterialMutation
from app.evaluation.repair_router import (
    RepairRouterFixtureExecutor,
    _build_fixture,
    load_repair_router_suite,
)
from app.evaluation.repair_router_contracts import (
    RepairExecutorScenario,
    RepairRouterEvalCase,
)
from app.planning.hard_validator import validate_hard_trip_plan
from app.planning.material_contracts import PlanningMaterialBundle
from app.planning.repair_contracts import (
    RepairExecutionResult,
    RepairExecutionStatus,
    RepairOutcome,
    RepairStopReason,
)
from app.planning.repair_router import RepairRouterProtocolError, run_repair_router


def _case(case_id: str) -> RepairRouterEvalCase:
    return next(item for item in load_repair_router_suite().cases if item.case_id == case_id)


def test_route_issue_runs_only_route_and_plan_once() -> None:
    case = _case("repair-router-route-success-v1")
    fixture = asyncio.run(_build_fixture(case))

    result = asyncio.run(
        run_repair_router(
            fixture.request,
            fixture.initial_plan,
            fixture.initial_materials,
            fixture.initial_opening,
            RepairRouterFixtureExecutor(case, fixture),
        )
    )

    assert result.outcome == RepairOutcome.REPAIRED
    assert result.stop_reason == RepairStopReason.FINALIZABLE
    assert result.final_report.can_finalize is True
    assert len(result.attempts) == 1
    assert result.attempts[0].repair_action == RepairAction.RERUN_ROUTE
    assert result.attempts[0].executed_nodes == (
        ResponsibleNode.ROUTE,
        ResponsibleNode.PLAN,
    )
    assert set(result.attempts[0].reused_nodes) == {
        ResponsibleNode.CONSTRAINT,
        ResponsibleNode.EXPLORE,
        ResponsibleNode.STAY,
        ResponsibleNode.WEATHER,
        ResponsibleNode.BUDGET,
    }
    assert result.attempts[0].resolved_issue_codes == ("route.missing_for_grounded_item",)


def test_warning_does_not_trigger_automatic_repair() -> None:
    case = _case("repair-router-opening-evidence-success-v1")
    clean_case = case.model_copy(
        update={
            "source_hard_validator_case_id": "hard-validator-confirmed-must-pass-v1",
            "executor_scenario": RepairExecutorScenario.UNUSED,
        }
    )
    clean_fixture = asyncio.run(_build_fixture(clean_case))
    executor = RepairRouterFixtureExecutor(clean_case, clean_fixture)

    result = asyncio.run(
        run_repair_router(
            clean_fixture.request,
            clean_fixture.initial_plan,
            clean_fixture.initial_materials,
            clean_fixture.initial_opening,
            executor,
        )
    )

    assert result.outcome == RepairOutcome.ALREADY_FINALIZABLE
    assert result.final_report.can_finalize is True
    assert tuple(item.severity.value for item in result.final_report.issues) == ("warning",)
    assert result.attempts == ()
    assert executor.call_count == 0


def test_budget_floor_stops_for_user_before_any_agent_call() -> None:
    case = _case("repair-router-budget-hitl-v1")
    fixture = asyncio.run(_build_fixture(case))
    executor = RepairRouterFixtureExecutor(case, fixture)

    result = asyncio.run(
        run_repair_router(
            fixture.request,
            fixture.initial_plan,
            fixture.initial_materials,
            fixture.initial_opening,
            executor,
        )
    )

    assert result.outcome == RepairOutcome.WAITING_FOR_USER
    assert result.stop_reason == RepairStopReason.USER_CONFIRMATION_REQUIRED
    assert result.requires_user_confirmation is True
    assert result.attempts == ()
    assert executor.call_count == 0


class _HiddenStayMutationExecutor:
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
        del request, issues, repair_action, action_attempt
        return RepairExecutionResult(
            status=RepairExecutionStatus.SUCCEEDED,
            materials=_mutate_materials(materials, HardMaterialMutation.STAY_CROSS_CITY),
            plan=plan,
            opening_hours=opening_hours,
            executed_nodes=(ResponsibleNode.ROUTE,),
        )


def test_router_rejects_hidden_mutation_of_a_reused_node() -> None:
    case = _case("repair-router-route-success-v1")
    fixture = asyncio.run(_build_fixture(case))

    with pytest.raises(RepairRouterProtocolError, match="reused node"):
        asyncio.run(
            run_repair_router(
                fixture.request,
                fixture.initial_plan,
                fixture.initial_materials,
                fixture.initial_opening,
                _HiddenStayMutationExecutor(),
            )
        )


class _TwoStageExecutor:
    def __init__(
        self,
        clean_materials: PlanningMaterialBundle,
        clean_plan: TripPlan,
    ) -> None:
        self._clean_materials = clean_materials
        self._clean_plan = clean_plan

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
        del request, issues, action_attempt
        if repair_action == RepairAction.RERUN_EXPLORE:
            return RepairExecutionResult(
                status=RepairExecutionStatus.SUCCEEDED,
                materials=self._clean_materials,
                plan=plan,
                opening_hours=opening_hours,
                executed_nodes=(ResponsibleNode.EXPLORE,),
            )
        return RepairExecutionResult(
            status=RepairExecutionStatus.SUCCEEDED,
            materials=materials,
            plan=self._clean_plan,
            opening_hours=opening_hours,
            executed_nodes=(ResponsibleNode.ROUTE, ResponsibleNode.PLAN),
        )


def test_upstream_issue_is_repaired_before_downstream_route_issue() -> None:
    route_case = _case("repair-router-route-success-v1")
    explore_case = _case("repair-router-poi-city-success-v1")
    route_fixture = asyncio.run(_build_fixture(route_case))
    explore_fixture = asyncio.run(_build_fixture(explore_case))

    result = asyncio.run(
        run_repair_router(
            route_fixture.request,
            route_fixture.initial_plan,
            explore_fixture.initial_materials,
            route_fixture.initial_opening,
            _TwoStageExecutor(explore_fixture.repaired_materials, route_fixture.repaired_plan),
        )
    )

    assert tuple(item.repair_action for item in result.attempts) == (
        RepairAction.RERUN_EXPLORE,
        RepairAction.RERUN_ROUTE,
    )
    assert result.final_report.can_finalize is True


class _GroupedIssueValidator:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(
        self,
        request: TripRequest,
        plan: TripPlan,
        materials: PlanningMaterialBundle,
        opening_hours: OpeningHoursEvidenceBundle,
    ) -> PlanValidationReport:
        self.call_count += 1
        report = validate_hard_trip_plan(request, plan, materials, opening_hours)
        if self.call_count != 1:
            return report
        second_issue = ValidationIssue(
            issue_id="fixture-second-route-issue",
            rule_code="fixture.route.second_issue",
            severity=IssueSeverity.ERROR,
            message="fixture 第二条路线错误",
            evidence=(
                ValidationEvidence(
                    field_path="plan.days",
                    description="fixture 分组证据",
                    observed_value="second-route-error",
                ),
            ),
            responsible_node=ResponsibleNode.ROUTE,
            repairable=True,
            repair_action=RepairAction.RERUN_ROUTE,
        )
        return report.model_copy(update={"issues": (*report.issues, second_issue)})


class _RecordingRouteExecutor:
    def __init__(self, repaired_plan: TripPlan) -> None:
        self._repaired_plan = repaired_plan
        self.received_issue_codes: tuple[str, ...] = ()

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
        del request, plan, repair_action, action_attempt
        self.received_issue_codes = tuple(item.rule_code for item in issues)
        return RepairExecutionResult(
            status=RepairExecutionStatus.SUCCEEDED,
            materials=materials,
            plan=self._repaired_plan,
            opening_hours=opening_hours,
            executed_nodes=(ResponsibleNode.ROUTE, ResponsibleNode.PLAN),
        )


def test_issues_with_the_same_action_are_grouped_into_one_attempt() -> None:
    case = _case("repair-router-route-success-v1")
    fixture = asyncio.run(_build_fixture(case))
    validator = _GroupedIssueValidator()
    executor = _RecordingRouteExecutor(fixture.repaired_plan)

    result = asyncio.run(
        run_repair_router(
            fixture.request,
            fixture.initial_plan,
            fixture.initial_materials,
            fixture.initial_opening,
            executor,
            validator=validator,
        )
    )

    assert executor.received_issue_codes == (
        "route.missing_for_grounded_item",
        "fixture.route.second_issue",
    )
    assert len(result.attempts) == 1
    assert result.final_report.can_finalize is True
