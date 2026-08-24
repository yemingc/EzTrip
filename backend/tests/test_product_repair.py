import asyncio
from pathlib import Path

import pytest

from app.domain.money import CostItem
from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import TripPlan
from app.domain.request import ConstraintSet, TripPace, TripRequest
from app.domain.sources import DataMode
from app.domain.validation import (
    IssueSeverity,
    RepairAction,
    ResponsibleNode,
    ValidationEvidence,
    ValidationIssue,
)
from app.evaluation import load_vertical_slice_suite
from app.evaluation.hard_validator import _mutate_materials, _mutate_plan
from app.evaluation.hard_validator_contracts import HardMaterialMutation, HardPlanMutation
from app.planning.hard_validator import validate_hard_trip_plan
from app.planning.material_contracts import (
    PlanningMaterialBundle,
    PlanningMaterialIssueCode,
    PlanningMaterialStatus,
)
from app.planning.product_graph import open_sqlite_product_runtime, should_skip_live_repair
from app.planning.product_repair import ProductRepairExecutor
from app.planning.repair_contracts import RepairOutcome
from app.planning.repair_router import run_repair_router
from app.planning.specialist_contracts import SpecialistFanoutResult
from app.planning.stateful_contracts import PlanningThreadStatus
from app.tasks.product_fixture import FixtureProductPlanningPipeline


def _fixture_request_and_costs() -> tuple[TripRequest, tuple[CostItem, ...]]:
    case = load_vertical_slice_suite().cases[0]
    payload = case.request.model_dump(mode="python")
    payload.update(
        {
            "request_id": "product-repair-beijing-two-day",
            "end_date": case.request.start_date.replace(day=3),
            "constraints": ConstraintSet(
                items=tuple(
                    item.model_copy(update={"value": "故宫博物院"})
                    if item.value == "故宫"
                    else item
                    for item in case.request.constraints.items
                    if item.value != "首都博物馆"
                )
            ),
        }
    )
    return TripRequest.model_validate(payload), case.cost_items[:4]


async def _product_artifacts() -> tuple[
    TripRequest,
    tuple[CostItem, ...],
    FixtureProductPlanningPipeline,
    PlanningMaterialBundle,
    TripPlan,
    OpeningHoursEvidenceBundle,
]:
    request, costs = _fixture_request_and_costs()
    pipeline = FixtureProductPlanningPipeline(request)
    specialists = await pipeline.run_specialists(request, data_mode=DataMode.FIXTURE)
    materials = await pipeline.build_materials(specialists)
    plan_result = pipeline.run_plan(request, materials)
    assert plan_result.plan is not None
    plan = TripPlan.model_validate(
        plan_result.plan.model_copy(update={"cost_items": costs}).model_dump(mode="python")
    )
    opening = pipeline.build_opening_hours(request, plan, data_mode=DataMode.FIXTURE)
    return request, costs, pipeline, materials, plan, opening


def test_product_repair_reschedules_inside_verified_opening_hours_without_model() -> None:
    async def scenario() -> None:
        request, _, pipeline, materials, plan, opening = await _product_artifacts()
        initial = validate_hard_trip_plan(request, plan, materials, opening)
        assert initial.can_finalize is False
        assert tuple(
            item.rule_code for item in initial.issues if item.severity.value == "error"
        ) == ("opening_hours.schedule_outside_verified_window",)

        result = await run_repair_router(
            request,
            plan,
            materials,
            opening,
            ProductRepairExecutor(pipeline),
        )

        assert result.outcome == RepairOutcome.REPAIRED
        assert result.final_report.can_finalize is True
        assert len(result.attempts) == 1
        attempt = result.attempts[0]
        assert attempt.repair_action == RepairAction.REPLAN_DAY
        assert attempt.executed_nodes == (ResponsibleNode.PLAN,)
        assert attempt.model_call_count == 0
        assert attempt.provider_call_count == 0
        assert attempt.plan_diff.changed_dates == (request.end_date,)
        temple = next(
            item for day in result.final_plan.days for item in day.items if item.title == "天坛公园"
        )
        assert temple.start_at.hour == 10

    asyncio.run(scenario())


def test_product_graph_skips_repair_when_hard_validation_is_finalizable(tmp_path: Path) -> None:
    class FinalizableFixturePipeline(FixtureProductPlanningPipeline):
        def build_opening_hours(
            self,
            request: TripRequest,
            plan: TripPlan,
            *,
            data_mode: DataMode,
        ) -> OpeningHoursEvidenceBundle:
            opening = super().build_opening_hours(request, plan, data_mode=data_mode)
            return OpeningHoursEvidenceBundle.model_validate(
                opening.model_copy(
                    update={
                        "items": tuple(
                            item.model_copy(update={"opens_at": item.opens_at.replace(hour=8)})
                            for item in opening.items
                        )
                    }
                ).model_dump(mode="python")
            )

    async def scenario() -> None:
        request, costs = _fixture_request_and_costs()
        pipeline = FinalizableFixturePipeline(request)
        nodes: list[str] = []

        async def capture(progress: object) -> None:
            node = progress.node
            nodes.append(node.value)

        async with open_sqlite_product_runtime(
            tmp_path / "product-finalizable.sqlite3",
            pipeline,
        ) as runtime:
            snapshot = await runtime.start_with_progress(
                "product-finalizable-thread",
                request,
                costs,
                data_mode=DataMode.FIXTURE,
                on_progress=capture,
            )

        assert snapshot.state.validation is not None
        assert snapshot.state.validation.can_finalize is True
        assert snapshot.state.repair is None
        assert "run_repair" not in nodes
        assert nodes[-1] == "prepare_human_review"

    asyncio.run(scenario())


def test_live_provider_fact_gaps_skip_expensive_automatic_repair() -> None:
    async def scenario() -> None:
        request, _, _, materials, plan, _ = await _product_artifacts()
        missing_opening = OpeningHoursEvidenceBundle(
            request_id=request.request_id,
            data_mode=materials.data_mode,
            items=(),
        )
        report = validate_hard_trip_plan(request, plan, materials, missing_opening)
        error_codes = {
            issue.rule_code for issue in report.issues if issue.severity == IssueSeverity.ERROR
        }

        assert error_codes == {"opening_hours.evidence_missing"}
        assert should_skip_live_repair(DataMode.LIVE, report) is True
        assert should_skip_live_repair(DataMode.FIXTURE, report) is False
        hard_conflict = report.model_copy(
            update={
                "issues": tuple(
                    issue.model_copy(update={"rule_code": "constraint.hard_avoid_scheduled"})
                    if issue.severity == IssueSeverity.ERROR
                    else issue
                    for issue in report.issues
                )
            }
        )
        assert should_skip_live_repair(DataMode.LIVE, hard_conflict) is False

    asyncio.run(scenario())


def test_product_graph_keeps_partial_materials_on_the_reviewable_draft_path(
    tmp_path: Path,
) -> None:
    class PartialMaterialsFixturePipeline(FixtureProductPlanningPipeline):
        async def build_materials(
            self,
            specialist_result: SpecialistFanoutResult,
        ) -> PlanningMaterialBundle:
            branches = tuple(
                branch.model_copy(
                    update={
                        "explore_result": branch.explore_result.model_copy(
                            update={"recommendations": branch.explore_result.recommendations[:3]}
                        )
                    }
                )
                if branch.explore_result is not None
                else branch
                for branch in specialist_result.branches
            )
            sparse_specialists = SpecialistFanoutResult.model_validate(
                specialist_result.model_copy(update={"branches": branches}).model_dump(
                    mode="python"
                )
            )
            return await super().build_materials(sparse_specialists)

    async def scenario() -> None:
        request, costs = _fixture_request_and_costs()
        request = request.model_copy(update={"pace": TripPace.RELAXED})
        pipeline = PartialMaterialsFixturePipeline(request)

        async with open_sqlite_product_runtime(
            tmp_path / "product-partial-materials.sqlite3",
            pipeline,
        ) as runtime:
            snapshot = await runtime.start_with_progress(
                "product-partial-materials-thread",
                request,
                costs,
                data_mode=DataMode.FIXTURE,
            )

        assert snapshot.state.materials is not None
        assert snapshot.state.materials.status == PlanningMaterialStatus.PARTIAL
        assert (
            PlanningMaterialIssueCode.ACTIVITY_COVERAGE_INSUFFICIENT
            in snapshot.state.materials.issues
        )
        assert snapshot.state.plan_agent is not None
        assert snapshot.state.plan_agent.plan is not None
        assert snapshot.state.status == PlanningThreadStatus.AWAITING_HUMAN_REVIEW

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mutation", "expected_action", "expected_nodes"),
    (
        (
            HardMaterialMutation.POI_CROSS_CITY,
            RepairAction.RERUN_EXPLORE,
            (ResponsibleNode.EXPLORE, ResponsibleNode.ROUTE, ResponsibleNode.PLAN),
        ),
        (
            HardMaterialMutation.STAY_CROSS_CITY,
            RepairAction.RERUN_STAY,
            (ResponsibleNode.STAY, ResponsibleNode.ROUTE, ResponsibleNode.PLAN),
        ),
    ),
)
def test_product_repair_reruns_only_target_specialist_and_downstream_nodes(
    mutation: HardMaterialMutation,
    expected_action: RepairAction,
    expected_nodes: tuple[ResponsibleNode, ...],
) -> None:
    async def scenario() -> None:
        request, _, pipeline, materials, plan, opening = await _product_artifacts()
        broken_materials = _mutate_materials(materials, mutation)
        result = await run_repair_router(
            request,
            plan,
            broken_materials,
            opening,
            ProductRepairExecutor(pipeline),
        )

        assert result.outcome == RepairOutcome.REPAIRED
        assert result.final_report.can_finalize is True
        assert result.attempts[0].repair_action == expected_action
        assert result.attempts[0].executed_nodes == expected_nodes
        assert result.attempts[0].model_call_count == 3
        assert ResponsibleNode.WEATHER in result.attempts[0].reused_nodes

    asyncio.run(scenario())


def test_product_route_repair_rebuilds_provider_matrix_and_replans() -> None:
    async def scenario() -> None:
        request, _, pipeline, materials, plan, opening = await _product_artifacts()
        broken_plan = _mutate_plan(plan, HardPlanMutation.MISSING_ROUTE)
        result = await run_repair_router(
            request,
            broken_plan,
            materials,
            opening,
            ProductRepairExecutor(pipeline),
        )

        assert result.outcome == RepairOutcome.REPAIRED
        assert result.attempts[0].repair_action == RepairAction.RERUN_ROUTE
        assert result.attempts[0].executed_nodes == (
            ResponsibleNode.ROUTE,
            ResponsibleNode.PLAN,
        )
        assert result.attempts[0].model_call_count == 1
        assert result.attempts[0].provider_call_count == (
            result.final_materials.route_matrix.provider_call_count
        )
        assert result.final_report.can_finalize is True

    asyncio.run(scenario())


def test_product_budget_executor_is_real_but_cannot_invent_missing_cost_facts() -> None:
    async def scenario() -> None:
        request, _, pipeline, materials, plan, opening = await _product_artifacts()
        issue = ValidationIssue(
            issue_id="product-budget-repair-fixture",
            rule_code="budget.incomplete_category_coverage",
            severity=IssueSeverity.ERROR,
            message="预算类别费用事实缺失。",
            evidence=(
                ValidationEvidence(
                    field_path="budget.missing_categories",
                    description="fixture 预算缺口",
                    observed_value="food",
                ),
            ),
            responsible_node=ResponsibleNode.BUDGET,
            repairable=True,
            repair_action=RepairAction.RECALCULATE_BUDGET,
        )
        execution = await ProductRepairExecutor(pipeline).repair(
            request,
            plan,
            materials,
            opening,
            (issue,),
            RepairAction.RECALCULATE_BUDGET,
            1,
        )

        assert execution.executed_nodes == (ResponsibleNode.BUDGET, ResponsibleNode.PLAN)
        assert execution.model_call_count == 1
        assert execution.provider_call_count == 0
        assert execution.plan.cost_items == plan.cost_items

    asyncio.run(scenario())
