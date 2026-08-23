import hashlib
import json
from datetime import date, datetime

from app.domain.planning import PlanVersion
from app.planning.stateful_contracts import StatefulPlanningSnapshot
from app.tasks.contracts import (
    PlanningTaskPlanDiff,
    PlanningTaskReviewDecisionRequest,
    PlanningTaskReviewOutcome,
)


def _sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_initial_plan_version(
    snapshot: StatefulPlanningSnapshot,
    *,
    created_at: datetime,
) -> PlanVersion:
    state = snapshot.state
    vertical_slice = state.vertical_slice
    if vertical_slice is None:
        raise ValueError("initial plan version requires a vertical slice result")
    plan = vertical_slice.plan
    plan_digest = _sha256(plan.model_dump(mode="json"))
    tool_snapshot_digest = _sha256(
        [candidate.model_dump(mode="json") for candidate in vertical_slice.upstream.candidates]
    )
    return PlanVersion(
        version_id=f"plan-version-{plan_digest[:16]}",
        plan=plan,
        version_number=1,
        created_at=created_at,
        input_constraint_sha256=_sha256(state.request.constraints.model_dump(mode="json")),
        tool_snapshot_ids=(f"tool-snapshot-{tool_snapshot_digest[:16]}",),
        model_versions={"planner": vertical_slice.planner.model},
        prompt_versions={"planner": vertical_slice.planner.prompt_version},
        change_summary=("生成初始 provider-grounded 行程草案。",),
        changed_dates=tuple(day.date for day in plan.days),
    )


def build_revised_plan_version(
    snapshot: StatefulPlanningSnapshot,
    previous: PlanVersion,
    *,
    created_at: datetime,
) -> PlanVersion:
    revision = snapshot.state.revision_result
    if revision is None:
        raise ValueError("revised plan version requires a persisted revision result")
    if revision.request.base_version_id != previous.version_id:
        raise ValueError("revision base version does not match the current task version")
    plan = revision.revised_plan
    plan_digest = _sha256(plan.model_dump(mode="json"))
    return PlanVersion(
        version_id=f"plan-version-{plan_digest[:16]}",
        plan=plan,
        version_number=previous.version_number + 1,
        based_on_version_id=previous.version_id,
        created_at=created_at,
        input_constraint_sha256=previous.input_constraint_sha256,
        tool_snapshot_ids=previous.tool_snapshot_ids,
        model_versions=previous.model_versions,
        prompt_versions=previous.prompt_versions,
        change_summary=(
            f"按用户确认的结构化请求将 {revision.request.target_date.isoformat()} "
            f"全部活动延后 {revision.request.shift_minutes} 分钟。",
            "复用原 Provider/Planner 产物并重新执行 deterministic-plan-validator-v1。",
        ),
        changed_dates=revision.diff.changed_dates,
    )


def build_review_outcome(
    snapshot: StatefulPlanningSnapshot,
    decision: PlanningTaskReviewDecisionRequest,
    versions: tuple[PlanVersion, ...],
) -> PlanningTaskReviewOutcome:
    review_decision = snapshot.state.review_decision
    if review_decision is None:
        raise ValueError("review outcome requires a persisted review decision")
    if (
        review_decision.review_id != decision.review_id
        or review_decision.action != decision.action
        or review_decision.reviewer_id != decision.reviewer_id
        or review_decision.comment != decision.comment
        or review_decision.revision_request != decision.revision_request
    ):
        raise ValueError("persisted review decision does not match the API submission")
    if not versions:
        raise ValueError("review outcome requires at least one plan version")
    from_version = versions[-1]
    to_version = versions[-1]
    changed_dates: tuple[date, ...] = ()
    rescheduled_item_ids: tuple[str, ...] = ()
    summary = {
        "approve_draft": "用户批准现有草案, 审核恢复没有修改行程结构。",
        "acknowledge_conflict": "用户确认已知冲突, 原草案保持不变且未被标记为可执行。",
        "request_revision": "结构化修改已应用到目标日期并生成新的待确认草案版本。",
        "cancel": "用户取消本次规划, 原草案仅保留为审计记录。",
    }[decision.action.value]
    revision = snapshot.state.revision_result
    if decision.action.value == "request_revision":
        if decision.revision_request is None or revision is None or len(versions) < 2:
            raise ValueError("request_revision outcome requires a revised plan version")
        from_version = versions[-2]
        to_version = versions[-1]
        if decision.revision_request.base_version_id != from_version.version_id:
            raise ValueError("revision decision does not reference the previous version")
        changed_dates = revision.diff.changed_dates
        rescheduled_item_ids = revision.diff.rescheduled_item_ids
    return PlanningTaskReviewOutcome(
        decision_id=decision.decision_id,
        review_id=decision.review_id,
        action=decision.action,
        reviewer_id=decision.reviewer_id,
        comment=decision.comment,
        decided_at=review_decision.decided_at,
        resulting_state_status=snapshot.state.status,
        plan_diff=PlanningTaskPlanDiff(
            from_version_id=from_version.version_id,
            to_version_id=to_version.version_id,
            plan_changed=from_version.version_id != to_version.version_id,
            changed_dates=changed_dates,
            added_item_ids=(),
            removed_item_ids=(),
            rescheduled_item_ids=rescheduled_item_ids,
            summary=(summary,),
        ),
    )
