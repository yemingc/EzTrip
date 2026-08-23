import asyncio

from app.domain.planning import PlanVersion
from app.planning.stateful_contracts import (
    HumanReviewAction,
    PlanningThreadStatus,
    StatefulPlanningNodeName,
)
from app.tasks.contracts import (
    PlanningTaskEvent,
    PlanningTaskEventKind,
    PlanningTaskFailure,
    PlanningTaskReviewDecisionRequest,
    PlanningTaskReviewOutcome,
    PlanningTaskSnapshot,
    PlanningTaskStatus,
    PlanningTaskSubmission,
    utc_now,
)
from app.tasks.plan_versions import (
    build_initial_plan_version,
    build_review_outcome,
    build_revised_plan_version,
)


class PlanningTaskNotFoundError(KeyError):
    """Raised when a task id is not present in the process-local task store."""


class PlanningTaskTransitionError(RuntimeError):
    """Raised when task state and event transitions diverge."""


class PlanningTaskReviewConflictError(RuntimeError):
    """Raised when a review decision conflicts with task or idempotency state."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class InMemoryPlanningTaskStore:
    """Process-local snapshots and replayable events for the first API increment."""

    def __init__(self) -> None:
        self._snapshots: dict[str, PlanningTaskSnapshot] = {}
        self._events: dict[str, list[PlanningTaskEvent]] = {}
        self._review_decisions: dict[tuple[str, str], PlanningTaskReviewDecisionRequest] = {}
        self._task_decision_ids: dict[str, str] = {}
        self._condition = asyncio.Condition()

    async def create(self, submission: PlanningTaskSubmission) -> PlanningTaskSnapshot:
        async with self._condition:
            if submission.task_id in self._snapshots:
                raise PlanningTaskTransitionError("planning task id already exists")
            now = utc_now()
            event = PlanningTaskEvent(
                event_id=self._event_id(submission.task_id, 1),
                sequence=1,
                task_id=submission.task_id,
                kind=PlanningTaskEventKind.TASK_CREATED,
                task_status=PlanningTaskStatus.QUEUED,
                occurred_at=now,
                message="规划任务已进入队列。",
            )
            snapshot = PlanningTaskSnapshot(
                task_id=submission.task_id,
                request_id=submission.request.request_id,
                data_mode=submission.data_mode,
                status=PlanningTaskStatus.QUEUED,
                created_at=now,
                updated_at=now,
                event_count=1,
            )
            self._snapshots[submission.task_id] = snapshot
            self._events[submission.task_id] = [event]
            self._condition.notify_all()
            return snapshot

    async def get(self, task_id: str) -> PlanningTaskSnapshot:
        async with self._condition:
            return self._require_snapshot(task_id)

    async def events_after(
        self,
        task_id: str,
        sequence: int,
    ) -> tuple[PlanningTaskEvent, ...]:
        async with self._condition:
            self._require_snapshot(task_id)
            return tuple(event for event in self._events[task_id] if event.sequence > sequence)

    async def wait_for_events(
        self,
        task_id: str,
        sequence: int,
        *,
        timeout_seconds: float,
    ) -> tuple[PlanningTaskEvent, ...]:
        async with self._condition:
            self._require_snapshot(task_id)
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: (
                            len(self._events[task_id]) > sequence
                            or self._snapshots[task_id].status
                            in {
                                PlanningTaskStatus.AWAITING_INPUT,
                                PlanningTaskStatus.SUCCEEDED,
                                PlanningTaskStatus.FAILED,
                            }
                        )
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                return ()
            return tuple(event for event in self._events[task_id] if event.sequence > sequence)

    async def start(self, task_id: str) -> PlanningTaskSnapshot:
        return await self._append(
            task_id,
            kind=PlanningTaskEventKind.TASK_STARTED,
            status=PlanningTaskStatus.RUNNING,
            message="规划工作流已开始执行。",
            allowed_from={PlanningTaskStatus.QUEUED},
        )

    async def record_node(
        self,
        task_id: str,
        *,
        node: StatefulPlanningNodeName,
        state_status: PlanningThreadStatus,
        message: str,
    ) -> PlanningTaskSnapshot:
        return await self._append(
            task_id,
            kind=PlanningTaskEventKind.GRAPH_NODE_COMPLETED,
            status=PlanningTaskStatus.RUNNING,
            message=message,
            allowed_from={PlanningTaskStatus.RUNNING},
            node=node,
            state_status=state_status,
        )

    async def submit_review(
        self,
        task_id: str,
        decision: PlanningTaskReviewDecisionRequest,
    ) -> bool:
        """Atomically accept one review decision; return True for an exact replay."""

        async with self._condition:
            key = (task_id, decision.decision_id)
            existing = self._review_decisions.get(key)
            if existing is not None:
                if existing != decision:
                    raise PlanningTaskReviewConflictError(
                        "review-decision-idempotency-conflict",
                        "同一 decision_id 已用于不同的审核内容。",
                    )
                return True

            previous = self._require_snapshot(task_id)
            accepted_decision_id = self._task_decision_ids.get(task_id)
            if accepted_decision_id is not None:
                raise PlanningTaskReviewConflictError(
                    "review-already-decided",
                    f"该任务已接受审核决定 {accepted_decision_id}。",
                )
            if previous.status != PlanningTaskStatus.AWAITING_INPUT or previous.result is None:
                raise PlanningTaskReviewConflictError(
                    "task-not-awaiting-review",
                    "该规划任务当前不在等待审核状态。",
                )
            review = previous.result.state.review_request
            if review is None or decision.review_id != review.review_id:
                raise PlanningTaskReviewConflictError(
                    "review-id-mismatch",
                    "review_id 与当前待处理审核不一致。",
                )
            if decision.action not in review.allowed_actions:
                raise PlanningTaskReviewConflictError(
                    "review-action-not-allowed",
                    f"当前审核不允许动作 {decision.action.value}。",
                )
            if decision.action == HumanReviewAction.REQUEST_REVISION:
                revision = decision.revision_request
                if revision is None or not previous.plan_versions:
                    raise PlanningTaskReviewConflictError(
                        "revision-request-missing",
                        "修改决定缺少结构化 revision request 或基准版本。",
                    )
                current_version = previous.plan_versions[-1]
                if (
                    revision.base_version_id != current_version.version_id
                    or revision.base_plan_id != current_version.plan.plan_id
                ):
                    raise PlanningTaskReviewConflictError(
                        "revision-base-version-mismatch",
                        "修改请求的基准版本不是当前计划版本。",
                    )
                target_days = tuple(
                    day for day in current_version.plan.days if day.date == revision.target_date
                )
                expected_target = tuple(item.item_id for day in target_days for item in day.items)
                expected_protected = tuple(
                    item.item_id
                    for day in current_version.plan.days
                    if day.date != revision.target_date
                    for item in day.items
                )
                if (
                    len(target_days) != 1
                    or revision.target_item_ids != expected_target
                    or revision.protected_item_ids != expected_protected
                ):
                    raise PlanningTaskReviewConflictError(
                        "revision-scope-mismatch",
                        "修改请求的目标或保护项目与当前计划不一致。",
                    )

            self._append_locked(
                task_id,
                kind=PlanningTaskEventKind.TASK_REVIEW_SUBMITTED,
                status=PlanningTaskStatus.RUNNING,
                message="审核决定已接收, 正在恢复原 LangGraph checkpoint。",
                allowed_from={PlanningTaskStatus.AWAITING_INPUT},
                review_id=decision.review_id,
                review_action=decision.action,
            )
            self._review_decisions[key] = decision
            self._task_decision_ids[task_id] = decision.decision_id
            self._condition.notify_all()
            return False

    async def await_input(
        self,
        task_id: str,
        *,
        result: object,
        review_id: str,
    ) -> PlanningTaskSnapshot:
        from app.planning.stateful_contracts import StatefulPlanningSnapshot

        snapshot = StatefulPlanningSnapshot.model_validate(result)
        plan_version = build_initial_plan_version(snapshot, created_at=utc_now())
        return await self._append(
            task_id,
            kind=PlanningTaskEventKind.TASK_AWAITING_INPUT,
            status=PlanningTaskStatus.AWAITING_INPUT,
            message="规划草案已生成, 正在等待用户审核。",
            allowed_from={PlanningTaskStatus.RUNNING},
            result=snapshot,
            review_id=review_id,
            plan_versions=(plan_version,),
        )

    async def succeed(
        self,
        task_id: str,
        *,
        result: object,
        review_decision: PlanningTaskReviewDecisionRequest | None = None,
    ) -> PlanningTaskSnapshot:
        from app.planning.stateful_contracts import StatefulPlanningSnapshot

        snapshot = StatefulPlanningSnapshot.model_validate(result)
        current = await self.get(task_id)
        review_outcome: PlanningTaskReviewOutcome | None = None
        plan_versions = current.plan_versions
        if review_decision is not None:
            if not plan_versions:
                raise PlanningTaskTransitionError("review completion requires a plan version")
            if review_decision.action == HumanReviewAction.REQUEST_REVISION:
                plan_versions = (
                    *plan_versions,
                    build_revised_plan_version(
                        snapshot,
                        plan_versions[-1],
                        created_at=utc_now(),
                    ),
                )
            review_outcome = build_review_outcome(
                snapshot,
                review_decision,
                plan_versions,
            )
        return await self._append(
            task_id,
            kind=PlanningTaskEventKind.TASK_SUCCEEDED,
            status=PlanningTaskStatus.SUCCEEDED,
            message="规划工作流已完成。",
            allowed_from={PlanningTaskStatus.RUNNING},
            result=snapshot,
            plan_versions=plan_versions,
            review_outcome=review_outcome,
        )

    async def fail(
        self,
        task_id: str,
        *,
        failure: PlanningTaskFailure,
    ) -> PlanningTaskSnapshot:
        return await self._append(
            task_id,
            kind=PlanningTaskEventKind.TASK_FAILED,
            status=PlanningTaskStatus.FAILED,
            message=failure.user_message,
            allowed_from={PlanningTaskStatus.RUNNING},
            failure=failure,
            error_code=failure.error_code,
        )

    async def _append(
        self,
        task_id: str,
        *,
        kind: PlanningTaskEventKind,
        status: PlanningTaskStatus,
        message: str,
        allowed_from: set[PlanningTaskStatus],
        node: StatefulPlanningNodeName | None = None,
        state_status: PlanningThreadStatus | None = None,
        review_id: str | None = None,
        review_action: HumanReviewAction | None = None,
        error_code: str | None = None,
        result: object | None = None,
        failure: PlanningTaskFailure | None = None,
        plan_versions: tuple[PlanVersion, ...] | None = None,
        review_outcome: PlanningTaskReviewOutcome | None = None,
    ) -> PlanningTaskSnapshot:
        async with self._condition:
            snapshot = self._append_locked(
                task_id,
                kind=kind,
                status=status,
                message=message,
                allowed_from=allowed_from,
                node=node,
                state_status=state_status,
                review_id=review_id,
                review_action=review_action,
                error_code=error_code,
                result=result,
                failure=failure,
                plan_versions=plan_versions,
                review_outcome=review_outcome,
            )
            self._condition.notify_all()
            return snapshot

    def _append_locked(
        self,
        task_id: str,
        *,
        kind: PlanningTaskEventKind,
        status: PlanningTaskStatus,
        message: str,
        allowed_from: set[PlanningTaskStatus],
        node: StatefulPlanningNodeName | None = None,
        state_status: PlanningThreadStatus | None = None,
        review_id: str | None = None,
        review_action: HumanReviewAction | None = None,
        error_code: str | None = None,
        result: object | None = None,
        failure: PlanningTaskFailure | None = None,
        plan_versions: tuple[PlanVersion, ...] | None = None,
        review_outcome: PlanningTaskReviewOutcome | None = None,
    ) -> PlanningTaskSnapshot:
        previous = self._require_snapshot(task_id)
        if previous.status not in allowed_from:
            raise PlanningTaskTransitionError(
                f"cannot append {kind.value} from {previous.status.value}"
            )
        sequence = previous.event_count + 1
        now = utc_now()
        event = PlanningTaskEvent(
            event_id=self._event_id(task_id, sequence),
            sequence=sequence,
            task_id=task_id,
            kind=kind,
            task_status=status,
            occurred_at=now,
            message=message,
            node=node,
            state_status=state_status,
            review_id=review_id,
            review_action=review_action,
            error_code=error_code,
        )
        payload = previous.model_dump(mode="python")
        payload.update(
            {
                "status": status,
                "updated_at": now,
                "event_count": sequence,
                "result": result,
                "failure": failure,
                "review_outcome": review_outcome,
            }
        )
        if plan_versions is not None:
            payload["plan_versions"] = plan_versions
        snapshot = PlanningTaskSnapshot.model_validate(payload)
        self._events[task_id].append(event)
        self._snapshots[task_id] = snapshot
        return snapshot

    def _require_snapshot(self, task_id: str) -> PlanningTaskSnapshot:
        try:
            return self._snapshots[task_id]
        except KeyError as error:
            raise PlanningTaskNotFoundError(task_id) from error

    @staticmethod
    def _event_id(task_id: str, sequence: int) -> str:
        return f"{task_id}-event-{sequence:06d}"
