import hashlib
import json
from datetime import date, datetime, timedelta
from time import perf_counter
from typing import Protocol

from app.agents.contracts import ExploreAgentResult, StayAgentResult
from app.agents.plan_agent_contracts import PlanAgentRunResult, PlanAgentRunStatus
from app.domain.context import PlannerContext
from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import DayPlan, ItineraryItem, TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.validation import RepairAction, ResponsibleNode, ValidationIssue
from app.itinerary_quality import EXCESSIVE_TRANSFER_MINUTES, major_activity_target
from app.planning.material_builder import allocate_budget
from app.planning.material_contracts import (
    BudgetAllocation,
    BudgetAllocationStatus,
    PlanningMaterialBundle,
    PlanningMaterialIssueCode,
    PlanningMaterialStatus,
    RouteMatrixStatus,
)
from app.planning.repair_contracts import (
    RepairExecutionResult,
    RepairExecutionStatus,
)
from app.planning.specialist_contracts import (
    SpecialistBranchResult,
    SpecialistBranchStatus,
    SpecialistFanoutResult,
    SpecialistFanoutStatus,
    SpecialistName,
)


class ProductRepairProtocolError(RuntimeError):
    """Raised when a product repair cannot preserve the bounded-repair contract."""


class ProductRepairPipeline(Protocol):
    async def rerun_explore(self, context: PlannerContext) -> ExploreAgentResult: ...

    async def rerun_stay(self, context: PlannerContext) -> StayAgentResult: ...

    async def build_materials(
        self,
        specialist_result: SpecialistFanoutResult,
    ) -> PlanningMaterialBundle: ...

    def run_plan(
        self,
        request: TripRequest,
        materials: PlanningMaterialBundle,
    ) -> PlanAgentRunResult: ...

    def build_opening_hours(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        data_mode: DataMode,
    ) -> OpeningHoursEvidenceBundle: ...


def _elapsed_ms(started: float) -> int:
    return max(round((perf_counter() - started) * 1000), 0)


def _fanout_status(branches: tuple[SpecialistBranchResult, ...]) -> SpecialistFanoutStatus:
    successful = sum(item.status == SpecialistBranchStatus.SUCCEEDED for item in branches)
    failed = sum(item.status == SpecialistBranchStatus.FAILED for item in branches)
    if successful == len(branches):
        return SpecialistFanoutStatus.COMPLETE
    if successful == 0 and failed == 0:
        return SpecialistFanoutStatus.BLOCKED
    if successful == 0:
        return SpecialistFanoutStatus.FAILED
    return SpecialistFanoutStatus.PARTIAL


def _replace_branch(
    result: SpecialistFanoutResult,
    replacement: SpecialistBranchResult,
) -> SpecialistFanoutResult:
    branches = tuple(
        replacement if item.specialist == replacement.specialist else item
        for item in result.branches
    )
    return SpecialistFanoutResult(
        request_id=result.request_id,
        context_id=result.context_id,
        data_mode=result.data_mode,
        status=_fanout_status(branches),
        planner_context=result.planner_context,
        branches=branches,
        total_model_call_count=sum(item.model_call_count for item in branches),
        total_provider_call_count=sum(item.provider_call_count for item in branches),
        fanout_latency_ms=max(item.elapsed_ms for item in branches),
    )


def _explore_branch(result: ExploreAgentResult, *, elapsed_ms: int) -> SpecialistBranchResult:
    return SpecialistBranchResult(
        specialist=SpecialistName.EXPLORE,
        status=SpecialistBranchStatus.SUCCEEDED,
        elapsed_ms=elapsed_ms,
        model_call_count=2,
        provider_call_count=len(result.queries),
        model_usages=tuple(
            usage for usage in (result.query_usage, result.selection_usage) if usage is not None
        ),
        explore_result=result,
    )


def _stay_branch(result: StayAgentResult, *, elapsed_ms: int) -> SpecialistBranchResult:
    return SpecialistBranchResult(
        specialist=SpecialistName.STAY,
        status=SpecialistBranchStatus.SUCCEEDED,
        elapsed_ms=elapsed_ms,
        model_call_count=2,
        provider_call_count=len(result.queries),
        model_usages=tuple(
            usage for usage in (result.query_usage, result.selection_usage) if usage is not None
        ),
        stay_result=result,
    )


def _material_issues(
    materials: PlanningMaterialBundle,
    budget: BudgetAllocation,
) -> tuple[PlanningMaterialIssueCode, ...]:
    issues: list[PlanningMaterialIssueCode] = []
    if materials.specialist_result.status != SpecialistFanoutStatus.COMPLETE:
        issues.append(PlanningMaterialIssueCode.SPECIALIST_INCOMPLETE)
    if materials.route_matrix.status not in {
        RouteMatrixStatus.COMPLETE,
        RouteMatrixStatus.NOT_REQUIRED,
    }:
        issues.append(PlanningMaterialIssueCode.ROUTE_MATRIX_INCOMPLETE)
    if budget.status != BudgetAllocationStatus.ALLOCATED:
        issues.append(PlanningMaterialIssueCode.BUDGET_NOT_ALLOCATED)
    if materials.shortlist.primary_stay is None:
        issues.append(PlanningMaterialIssueCode.STAY_ANCHOR_MISSING)
    if materials.planner_context.pace is not None and len(
        materials.shortlist.poi_candidates
    ) < major_activity_target(
        materials.planner_context.day_count,
        materials.planner_context.pace,
    ):
        issues.append(PlanningMaterialIssueCode.ACTIVITY_COVERAGE_INSUFFICIENT)
    if materials.planner_context.pace is not None and any(
        edge.route is not None and edge.route.duration_minutes > EXCESSIVE_TRANSFER_MINUTES
        for edge in materials.route_matrix.edges
    ):
        issues.append(PlanningMaterialIssueCode.EXCESSIVE_TRANSFER)
    return tuple(issues)


def _replace_budget(
    materials: PlanningMaterialBundle,
    budget: BudgetAllocation,
) -> PlanningMaterialBundle:
    issues = _material_issues(materials, budget)
    explore = next(
        item
        for item in materials.specialist_result.branches
        if item.specialist == SpecialistName.EXPLORE
    )
    status = PlanningMaterialStatus.READY
    if (
        materials.specialist_result.status == SpecialistFanoutStatus.BLOCKED
        or materials.route_matrix.status
        in {RouteMatrixStatus.BLOCKED, RouteMatrixStatus.UNAVAILABLE}
        or explore.status != SpecialistBranchStatus.SUCCEEDED
    ):
        status = PlanningMaterialStatus.BLOCKED
    elif issues:
        status = PlanningMaterialStatus.PARTIAL
    return PlanningMaterialBundle(
        request_id=materials.request_id,
        context_id=materials.context_id,
        data_mode=materials.data_mode,
        status=status,
        issues=issues,
        planner_context=materials.planner_context,
        specialist_result=materials.specialist_result,
        shortlist=materials.shortlist,
        route_matrix=materials.route_matrix,
        budget_allocation=budget,
        budget_estimate=materials.budget_estimate,
    )


def _plan_from_result(result: PlanAgentRunResult, *, cost_source: TripPlan) -> TripPlan:
    if result.status != PlanAgentRunStatus.PLANNED or result.plan is None:
        raise ProductRepairProtocolError("repair Plan Agent did not produce a plan")
    return TripPlan.model_validate(
        result.plan.model_copy(update={"cost_items": cost_source.cost_items}).model_dump(
            mode="python"
        )
    )


def _issue_item_ids(issues: tuple[ValidationIssue, ...]) -> set[str]:
    item_ids: set[str] = set()
    for issue in issues:
        for evidence in issue.evidence:
            value = evidence.observed_value
            if not isinstance(value, str):
                continue
            if issue.rule_code == "opening_hours.schedule_outside_verified_window":
                item_ids.update(item for item in value.split(",") if item)
            if issue.rule_code == "route.insufficient_transfer_window":
                item_ids.update(part.split(":", maxsplit=1)[0] for part in value.split(";") if part)
    return item_ids


def _schedule_repair_plan_id(
    plan: TripPlan,
    issues: tuple[ValidationIssue, ...],
    action_attempt: int,
    days: tuple[DayPlan, ...],
) -> str:
    payload = {
        "base_plan_id": plan.plan_id,
        "issue_codes": [item.rule_code for item in issues],
        "action_attempt": action_attempt,
        "days": [item.model_dump(mode="json") for item in days],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"trip-plan-repair-{digest}"


def repair_plan_schedule(
    plan: TripPlan,
    opening_hours: OpeningHoursEvidenceBundle,
    issues: tuple[ValidationIssue, ...],
    *,
    action_attempt: int,
) -> TripPlan | None:
    supported_codes = {
        "opening_hours.schedule_outside_verified_window",
        "route.insufficient_transfer_window",
    }
    issue_codes = {item.rule_code for item in issues}
    if not issue_codes or not issue_codes <= supported_codes:
        return None
    affected_ids = _issue_item_ids(issues)
    if not affected_ids:
        return None
    windows: dict[tuple[str, date], list[tuple[datetime, datetime]]] = {}
    for evidence in opening_hours.items:
        windows.setdefault((evidence.candidate_id, evidence.service_date), []).append(
            (evidence.opens_at, evidence.closes_at)
        )

    changed = False
    repaired_days: list[DayPlan] = []
    for day in plan.days:
        previous_end: datetime | None = None
        repaired_items: list[ItineraryItem] = []
        for item in day.items:
            duration = item.end_at - item.start_at
            minimum_start = item.start_at
            if previous_end is not None:
                transfer_minutes = (
                    item.route_from_previous.duration_minutes
                    if item.route_from_previous is not None
                    else 0
                )
                minimum_start = max(
                    minimum_start,
                    previous_end + timedelta(minutes=transfer_minutes),
                )
            if item.item_id in affected_ids and item.candidate_id is not None:
                candidate_windows = sorted(
                    windows.get((item.candidate_id, day.date), []),
                    key=lambda value: value[0],
                )
                if "opening_hours.schedule_outside_verified_window" in issue_codes:
                    fitting_starts = tuple(
                        max(minimum_start, opens_at)
                        for opens_at, closes_at in candidate_windows
                        if max(minimum_start, opens_at) + duration <= closes_at
                    )
                    if not fitting_starts:
                        raise ProductRepairProtocolError(
                            "no verified same-day opening window can fit the activity"
                        )
                    minimum_start = min(fitting_starts)
            new_end = minimum_start + duration
            if minimum_start != item.start_at:
                changed = True
                note = "Hard Validator 触发确定性同日排程修复, 未调用模型。"
                notes = item.notes if note in item.notes else (*item.notes, note)
                item = ItineraryItem.model_validate(
                    item.model_copy(
                        update={
                            "start_at": minimum_start,
                            "end_at": new_end,
                            "notes": notes,
                        }
                    ).model_dump(mode="python")
                )
            repaired_items.append(item)
            previous_end = item.end_at
        repaired_days.append(
            DayPlan.model_validate(
                day.model_copy(
                    update={
                        "items": tuple(repaired_items),
                        "departure_from_stay_at": (
                            repaired_items[0].start_at
                            - timedelta(
                                minutes=repaired_items[0].route_from_previous.duration_minutes
                            )
                            if repaired_items and repaired_items[0].route_from_previous is not None
                            else day.departure_from_stay_at
                        ),
                    }
                ).model_dump(mode="python")
            )
        )
    if not changed:
        return None
    days = tuple(repaired_days)
    return TripPlan.model_validate(
        plan.model_copy(
            update={
                "plan_id": _schedule_repair_plan_id(plan, issues, action_attempt, days),
                "days": days,
            }
        ).model_dump(mode="python")
    )


class ProductRepairExecutor:
    """Execute only the responsibility nodes allowed by Repair Router."""

    def __init__(self, pipeline: ProductRepairPipeline) -> None:
        self._pipeline = pipeline

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
        if repair_action == RepairAction.RERUN_EXPLORE:
            started = perf_counter()
            explore = await self._pipeline.rerun_explore(materials.planner_context)
            branch = _explore_branch(explore, elapsed_ms=_elapsed_ms(started))
            specialists = _replace_branch(materials.specialist_result, branch)
            repaired_materials = await self._pipeline.build_materials(specialists)
            plan_result = self._pipeline.run_plan(request, repaired_materials)
            repaired_plan = _plan_from_result(plan_result, cost_source=plan)
            repaired_opening = self._pipeline.build_opening_hours(
                request,
                repaired_plan,
                data_mode=materials.data_mode,
            )
            return RepairExecutionResult(
                status=RepairExecutionStatus.SUCCEEDED,
                materials=repaired_materials,
                plan=repaired_plan,
                opening_hours=repaired_opening,
                executed_nodes=(
                    ResponsibleNode.EXPLORE,
                    ResponsibleNode.ROUTE,
                    ResponsibleNode.PLAN,
                ),
                model_call_count=branch.model_call_count + plan_result.model_call_count,
                provider_call_count=(
                    branch.provider_call_count + repaired_materials.route_matrix.provider_call_count
                ),
            )

        if repair_action == RepairAction.RERUN_STAY:
            started = perf_counter()
            stay = await self._pipeline.rerun_stay(materials.planner_context)
            branch = _stay_branch(stay, elapsed_ms=_elapsed_ms(started))
            specialists = _replace_branch(materials.specialist_result, branch)
            repaired_materials = await self._pipeline.build_materials(specialists)
            plan_result = self._pipeline.run_plan(request, repaired_materials)
            return RepairExecutionResult(
                status=RepairExecutionStatus.SUCCEEDED,
                materials=repaired_materials,
                plan=_plan_from_result(plan_result, cost_source=plan),
                opening_hours=opening_hours,
                executed_nodes=(
                    ResponsibleNode.STAY,
                    ResponsibleNode.ROUTE,
                    ResponsibleNode.PLAN,
                ),
                model_call_count=branch.model_call_count + plan_result.model_call_count,
                provider_call_count=(
                    branch.provider_call_count + repaired_materials.route_matrix.provider_call_count
                ),
            )

        if repair_action == RepairAction.RERUN_ROUTE:
            repaired_materials = await self._pipeline.build_materials(materials.specialist_result)
            plan_result = self._pipeline.run_plan(request, repaired_materials)
            return RepairExecutionResult(
                status=RepairExecutionStatus.SUCCEEDED,
                materials=repaired_materials,
                plan=_plan_from_result(plan_result, cost_source=plan),
                opening_hours=opening_hours,
                executed_nodes=(ResponsibleNode.ROUTE, ResponsibleNode.PLAN),
                model_call_count=plan_result.model_call_count,
                provider_call_count=repaired_materials.route_matrix.provider_call_count,
            )

        if repair_action == RepairAction.RECALCULATE_BUDGET:
            budget = allocate_budget(materials.planner_context)
            repaired_materials = _replace_budget(materials, budget)
            plan_result = self._pipeline.run_plan(request, repaired_materials)
            return RepairExecutionResult(
                status=RepairExecutionStatus.SUCCEEDED,
                materials=repaired_materials,
                plan=_plan_from_result(plan_result, cost_source=plan),
                opening_hours=opening_hours,
                executed_nodes=(ResponsibleNode.BUDGET, ResponsibleNode.PLAN),
                model_call_count=plan_result.model_call_count,
            )

        if repair_action == RepairAction.REPLAN_DAY:
            deterministic_plan = repair_plan_schedule(
                plan,
                opening_hours,
                issues,
                action_attempt=action_attempt,
            )
            if deterministic_plan is not None:
                return RepairExecutionResult(
                    status=RepairExecutionStatus.SUCCEEDED,
                    materials=materials,
                    plan=deterministic_plan,
                    opening_hours=opening_hours,
                    executed_nodes=(ResponsibleNode.PLAN,),
                )
            plan_result = self._pipeline.run_plan(request, materials)
            return RepairExecutionResult(
                status=RepairExecutionStatus.SUCCEEDED,
                materials=materials,
                plan=_plan_from_result(plan_result, cost_source=plan),
                opening_hours=opening_hours,
                executed_nodes=(ResponsibleNode.PLAN,),
                model_call_count=plan_result.model_call_count,
            )

        raise ProductRepairProtocolError(f"product repair does not support {repair_action.value}")


__all__ = [
    "ProductRepairExecutor",
    "ProductRepairPipeline",
    "ProductRepairProtocolError",
    "repair_plan_schedule",
]
