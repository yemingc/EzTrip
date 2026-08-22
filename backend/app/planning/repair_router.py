import hashlib
import json
from typing import Protocol

from pydantic import BaseModel

from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import TripPlan
from app.domain.request import TripRequest
from app.domain.validation import (
    IssueSeverity,
    PlanValidationReport,
    RepairAction,
    ResponsibleNode,
    ValidationIssue,
)
from app.planning.hard_validator import validate_hard_trip_plan
from app.planning.material_contracts import PlanningMaterialBundle
from app.planning.repair_contracts import (
    RepairArtifactHashes,
    RepairAttemptTrace,
    RepairExecutionResult,
    RepairExecutionStatus,
    RepairOutcome,
    RepairPlanDiff,
    RepairRetryCount,
    RepairRouterResult,
    RepairStopReason,
)
from app.planning.specialist_contracts import SpecialistName

REPAIR_ROUTER_VERSION = "repair-router-v1"
MAX_REPAIR_ATTEMPTS_PER_ACTION = 2

PIPELINE_NODES = (
    ResponsibleNode.CONSTRAINT,
    ResponsibleNode.EXPLORE,
    ResponsibleNode.STAY,
    ResponsibleNode.WEATHER,
    ResponsibleNode.ROUTE,
    ResponsibleNode.BUDGET,
    ResponsibleNode.PLAN,
)

ACTION_NODE = {
    RepairAction.RERUN_CONSTRAINT: ResponsibleNode.CONSTRAINT,
    RepairAction.RERUN_EXPLORE: ResponsibleNode.EXPLORE,
    RepairAction.RERUN_STAY: ResponsibleNode.STAY,
    RepairAction.RERUN_ROUTE: ResponsibleNode.ROUTE,
    RepairAction.RECALCULATE_BUDGET: ResponsibleNode.BUDGET,
    RepairAction.REPLAN_DAY: ResponsibleNode.PLAN,
}

ACTION_PRIORITY = {
    RepairAction.RERUN_CONSTRAINT: 0,
    RepairAction.RERUN_EXPLORE: 10,
    RepairAction.RERUN_STAY: 20,
    RepairAction.RERUN_ROUTE: 30,
    RepairAction.RECALCULATE_BUDGET: 40,
    RepairAction.REPLAN_DAY: 50,
}

ACTION_ALLOWED_NODES = {
    RepairAction.RERUN_CONSTRAINT: frozenset(
        {
            ResponsibleNode.CONSTRAINT,
            ResponsibleNode.EXPLORE,
            ResponsibleNode.STAY,
            ResponsibleNode.ROUTE,
            ResponsibleNode.BUDGET,
            ResponsibleNode.PLAN,
        }
    ),
    RepairAction.RERUN_EXPLORE: frozenset(
        {ResponsibleNode.EXPLORE, ResponsibleNode.ROUTE, ResponsibleNode.PLAN}
    ),
    RepairAction.RERUN_STAY: frozenset(
        {ResponsibleNode.STAY, ResponsibleNode.ROUTE, ResponsibleNode.PLAN}
    ),
    RepairAction.RERUN_ROUTE: frozenset({ResponsibleNode.ROUTE, ResponsibleNode.PLAN}),
    RepairAction.RECALCULATE_BUDGET: frozenset({ResponsibleNode.BUDGET, ResponsibleNode.PLAN}),
    RepairAction.REPLAN_DAY: frozenset({ResponsibleNode.PLAN}),
}


class RepairRouterProtocolError(RuntimeError):
    """Raised when a repair executor violates the bounded-repair contract."""


class RepairExecutor(Protocol):
    async def repair(
        self,
        request: TripRequest,
        plan: TripPlan,
        materials: PlanningMaterialBundle,
        opening_hours: OpeningHoursEvidenceBundle,
        issues: tuple[ValidationIssue, ...],
        repair_action: RepairAction,
        action_attempt: int,
    ) -> RepairExecutionResult: ...


class HardPlanValidator(Protocol):
    def __call__(
        self,
        request: TripRequest,
        plan: TripPlan,
        materials: PlanningMaterialBundle,
        opening_hours: OpeningHoursEvidenceBundle,
    ) -> PlanValidationReport: ...


def _semantic_sha256(value: BaseModel) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _artifact_hashes(
    materials: PlanningMaterialBundle,
    plan: TripPlan,
    opening_hours: OpeningHoursEvidenceBundle,
) -> RepairArtifactHashes:
    return RepairArtifactHashes(
        materials_sha256=_semantic_sha256(materials),
        plan_sha256=_semantic_sha256(plan),
        opening_hours_sha256=_semantic_sha256(opening_hours),
    )


def _component_hashes(
    materials: PlanningMaterialBundle,
    plan: TripPlan,
) -> dict[ResponsibleNode, str]:
    branches = {item.specialist: item for item in materials.specialist_result.branches}
    return {
        ResponsibleNode.CONSTRAINT: _semantic_sha256(materials.planner_context),
        ResponsibleNode.EXPLORE: _semantic_sha256(branches[SpecialistName.EXPLORE]),
        ResponsibleNode.STAY: _semantic_sha256(branches[SpecialistName.STAY]),
        ResponsibleNode.WEATHER: _semantic_sha256(branches[SpecialistName.WEATHER]),
        ResponsibleNode.ROUTE: _semantic_sha256(materials.route_matrix),
        ResponsibleNode.BUDGET: _semantic_sha256(materials.budget_allocation),
        ResponsibleNode.PLAN: _semantic_sha256(plan),
    }


def _candidate_ids(plan: TripPlan) -> tuple[str, ...]:
    return tuple(
        item.candidate_id
        for day in plan.days
        for item in day.items
        if item.candidate_id is not None
    )


def _plan_diff(before: TripPlan, after: TripPlan) -> RepairPlanDiff:
    before_days = {item.date: item for item in before.days}
    after_days = {item.date: item for item in after.days}
    changed_dates = tuple(
        day
        for day in sorted(set(before_days) | set(after_days))
        if before_days.get(day) != after_days.get(day)
    )
    before_candidates = set(_candidate_ids(before))
    after_candidates = set(_candidate_ids(after))
    return RepairPlanDiff(
        changed_dates=changed_dates,
        added_candidate_ids=tuple(sorted(after_candidates - before_candidates)),
        removed_candidate_ids=tuple(sorted(before_candidates - after_candidates)),
        total_cost_minimum_before=before.total_cost_minimum,
        total_cost_minimum_after=after.total_cost_minimum,
        total_cost_maximum_before=before.total_cost_maximum,
        total_cost_maximum_after=after.total_cost_maximum,
    )


def _error_issues(report: PlanValidationReport) -> tuple[ValidationIssue, ...]:
    return tuple(item for item in report.issues if item.severity == IssueSeverity.ERROR)


def _stable_execution_error(error: Exception) -> str:
    material = f"{error.__class__.__module__}|{error.__class__.__qualname__}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"repair-executor-error-{digest}"


def _validate_identity(
    request: TripRequest,
    result: RepairExecutionResult,
) -> None:
    if (
        result.materials.request_id != request.request_id
        or result.plan.request_id != request.request_id
        or result.opening_hours.request_id != request.request_id
    ):
        raise RepairRouterProtocolError("repair executor changed request identity")
    if result.materials.data_mode != result.opening_hours.data_mode:
        raise RepairRouterProtocolError("repair executor mixed planning data modes")


def _validate_successful_execution(
    action: RepairAction,
    before_materials: PlanningMaterialBundle,
    before_plan: TripPlan,
    before_opening: OpeningHoursEvidenceBundle,
    result: RepairExecutionResult,
) -> None:
    allowed_nodes = ACTION_ALLOWED_NODES[action]
    executed_nodes = set(result.executed_nodes)
    if ACTION_NODE[action] not in executed_nodes:
        raise RepairRouterProtocolError("successful repair did not execute its responsible node")
    if not executed_nodes <= allowed_nodes:
        raise RepairRouterProtocolError(
            "repair executor ran a node outside the action dependency scope"
        )

    before_components = _component_hashes(before_materials, before_plan)
    after_components = _component_hashes(result.materials, result.plan)
    changed_nodes = {
        node for node in PIPELINE_NODES if before_components[node] != after_components[node]
    }
    if not changed_nodes <= executed_nodes:
        raise RepairRouterProtocolError("repair executor changed an artifact for a reused node")
    opening_changed = _semantic_sha256(before_opening) != _semantic_sha256(result.opening_hours)
    if opening_changed and ResponsibleNode.EXPLORE not in executed_nodes:
        raise RepairRouterProtocolError(
            "only an executed Explore node may refresh opening evidence"
        )


def _validate_execution_result(
    request: TripRequest,
    action: RepairAction,
    before_materials: PlanningMaterialBundle,
    before_plan: TripPlan,
    before_opening: OpeningHoursEvidenceBundle,
    result: RepairExecutionResult,
) -> None:
    _validate_identity(request, result)
    if not set(result.executed_nodes) <= ACTION_ALLOWED_NODES[action]:
        raise RepairRouterProtocolError(
            "repair executor reported a node outside the action dependency scope"
        )
    if result.status == RepairExecutionStatus.FAILED:
        if (
            result.materials != before_materials
            or result.plan != before_plan
            or result.opening_hours != before_opening
        ):
            raise RepairRouterProtocolError("failed repair executions must discard partial output")
        return
    _validate_successful_execution(
        action,
        before_materials,
        before_plan,
        before_opening,
        result,
    )


def _retry_counts(attempts: tuple[RepairAttemptTrace, ...]) -> tuple[RepairRetryCount, ...]:
    counts: dict[RepairAction, int] = {}
    for attempt in attempts:
        counts[attempt.repair_action] = counts.get(attempt.repair_action, 0) + 1
    return tuple(
        RepairRetryCount(repair_action=action, attempt_count=count)
        for action, count in counts.items()
    )


def _finish(
    *,
    request: TripRequest,
    outcome: RepairOutcome,
    stop_reason: RepairStopReason,
    initial_report: PlanValidationReport,
    final_report: PlanValidationReport,
    materials: PlanningMaterialBundle,
    plan: TripPlan,
    opening_hours: OpeningHoursEvidenceBundle,
    attempts: list[RepairAttemptTrace],
    requires_user_confirmation: bool = False,
) -> RepairRouterResult:
    frozen_attempts = tuple(attempts)
    return RepairRouterResult(
        request_id=request.request_id,
        outcome=outcome,
        stop_reason=stop_reason,
        initial_report=initial_report,
        final_report=final_report,
        final_materials=materials,
        final_plan=plan,
        final_opening_hours=opening_hours,
        attempts=frozen_attempts,
        retry_counts=_retry_counts(frozen_attempts),
        pending_error_codes=tuple(item.rule_code for item in _error_issues(final_report)),
        requires_user_confirmation=requires_user_confirmation,
        total_model_call_count=sum(item.model_call_count for item in frozen_attempts),
        total_provider_call_count=sum(item.provider_call_count for item in frozen_attempts),
    )


async def run_repair_router(
    request: TripRequest,
    plan: TripPlan,
    materials: PlanningMaterialBundle,
    opening_hours: OpeningHoursEvidenceBundle,
    executor: RepairExecutor,
    *,
    validator: HardPlanValidator = validate_hard_trip_plan,
    max_attempts_per_action: int = MAX_REPAIR_ATTEMPTS_PER_ACTION,
) -> RepairRouterResult:
    """Run bounded, issue-directed repairs while preserving unaffected node outputs."""

    if not 1 <= max_attempts_per_action <= MAX_REPAIR_ATTEMPTS_PER_ACTION:
        raise ValueError("repair attempt limit must be one or two")
    if (
        plan.request_id != request.request_id
        or materials.request_id != request.request_id
        or opening_hours.request_id != request.request_id
    ):
        raise RepairRouterProtocolError("repair inputs must share one request identity")
    if materials.data_mode != opening_hours.data_mode:
        raise RepairRouterProtocolError("repair inputs must share one data mode")

    current_plan = plan
    current_materials = materials
    current_opening = opening_hours
    current_report = validator(
        request,
        current_plan,
        current_materials,
        current_opening,
    )
    initial_report = current_report
    attempts: list[RepairAttemptTrace] = []
    action_counts: dict[RepairAction, int] = {}

    while True:
        errors = _error_issues(current_report)
        if not errors:
            outcome = RepairOutcome.REPAIRED if attempts else RepairOutcome.ALREADY_FINALIZABLE
            return _finish(
                request=request,
                outcome=outcome,
                stop_reason=RepairStopReason.FINALIZABLE,
                initial_report=initial_report,
                final_report=current_report,
                materials=current_materials,
                plan=current_plan,
                opening_hours=current_opening,
                attempts=attempts,
            )

        confirmation_issues = tuple(
            item
            for item in errors
            if item.repair_action == RepairAction.ASK_USER or item.requires_user_confirmation
        )
        if confirmation_issues:
            return _finish(
                request=request,
                outcome=RepairOutcome.WAITING_FOR_USER,
                stop_reason=RepairStopReason.USER_CONFIRMATION_REQUIRED,
                initial_report=initial_report,
                final_report=current_report,
                materials=current_materials,
                plan=current_plan,
                opening_hours=current_opening,
                attempts=attempts,
                requires_user_confirmation=True,
            )

        unrepairable = tuple(
            item for item in errors if not item.repairable or item.repair_action not in ACTION_NODE
        )
        if unrepairable:
            return _finish(
                request=request,
                outcome=RepairOutcome.UNRESOLVED,
                stop_reason=RepairStopReason.UNREPAIRABLE_ISSUE,
                initial_report=initial_report,
                final_report=current_report,
                materials=current_materials,
                plan=current_plan,
                opening_hours=current_opening,
                attempts=attempts,
            )

        action = min(
            (item.repair_action for item in errors),
            key=lambda item: ACTION_PRIORITY[item],
        )
        action_attempt = action_counts.get(action, 0) + 1
        if action_attempt > max_attempts_per_action:
            return _finish(
                request=request,
                outcome=RepairOutcome.UNRESOLVED,
                stop_reason=RepairStopReason.RETRY_LIMIT_REACHED,
                initial_report=initial_report,
                final_report=current_report,
                materials=current_materials,
                plan=current_plan,
                opening_hours=current_opening,
                attempts=attempts,
            )
        trigger_issues = tuple(item for item in errors if item.repair_action == action)
        before_hashes = _artifact_hashes(current_materials, current_plan, current_opening)
        before_error_codes = tuple(item.rule_code for item in errors)
        try:
            execution = await executor.repair(
                request,
                current_plan,
                current_materials,
                current_opening,
                trigger_issues,
                action,
                action_attempt,
            )
        except Exception as error:
            execution = RepairExecutionResult(
                status=RepairExecutionStatus.FAILED,
                materials=current_materials,
                plan=current_plan,
                opening_hours=current_opening,
                error_code=_stable_execution_error(error),
            )
        _validate_execution_result(
            request,
            action,
            current_materials,
            current_plan,
            current_opening,
            execution,
        )

        next_report = validator(
            request,
            execution.plan,
            execution.materials,
            execution.opening_hours,
        )
        after_errors = _error_issues(next_report)
        after_error_codes = tuple(item.rule_code for item in after_errors)
        before_set = set(before_error_codes)
        after_set = set(after_error_codes)
        executed_nodes = execution.executed_nodes
        reused_nodes = tuple(item for item in PIPELINE_NODES if item not in executed_nodes)
        attempts.append(
            RepairAttemptTrace(
                attempt_index=len(attempts) + 1,
                action_attempt=action_attempt,
                repair_action=action,
                responsible_node=ACTION_NODE[action],
                trigger_issue_codes=tuple(item.rule_code for item in trigger_issues),
                execution_status=execution.status,
                executed_nodes=executed_nodes,
                reused_nodes=reused_nodes,
                before_error_codes=before_error_codes,
                after_error_codes=after_error_codes,
                resolved_issue_codes=tuple(
                    item for item in before_error_codes if item not in after_set
                ),
                introduced_issue_codes=tuple(
                    item for item in after_error_codes if item not in before_set
                ),
                before_hashes=before_hashes,
                after_hashes=_artifact_hashes(
                    execution.materials,
                    execution.plan,
                    execution.opening_hours,
                ),
                plan_diff=_plan_diff(current_plan, execution.plan),
                model_call_count=execution.model_call_count,
                provider_call_count=execution.provider_call_count,
                error_code=execution.error_code,
            )
        )
        action_counts[action] = action_attempt
        current_materials = execution.materials
        current_plan = execution.plan
        current_opening = execution.opening_hours
        current_report = next_report
