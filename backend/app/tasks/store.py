import asyncio
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, model_validator

from app.domain.base import DomainModel, Identifier
from app.domain.planning import ActivityKind, PlanVersion
from app.itinerary_quality import is_meal_candidate
from app.planning.product_contracts import ProductPlanningNodeName, ProductPlanningSnapshot
from app.planning.revision_contracts import PlanRevisionOperation
from app.planning.specialist_contracts import SpecialistName
from app.planning.stateful_contracts import (
    HumanReviewAction,
    PlanningThreadStatus,
    StatefulPlanningNodeName,
    StatefulPlanningSnapshot,
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

PlanningResultSnapshot = StatefulPlanningSnapshot | ProductPlanningSnapshot
PLANNING_RESULT_ADAPTER: TypeAdapter[PlanningResultSnapshot] = TypeAdapter(PlanningResultSnapshot)


class PlanningTaskNotFoundError(KeyError):
    """Raised when a task id is not present in the configured task store."""


class PlanningTaskTransitionError(RuntimeError):
    """Raised when task state and event transitions diverge."""


class PlanningTaskPersistenceError(RuntimeError):
    """Raised when the local durable task ledger cannot be loaded or committed."""


class PlanningTaskReviewConflictError(RuntimeError):
    """Raised when a review decision conflicts with task or idempotency state."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class _PersistedPlanningTaskRecord(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    snapshot: PlanningTaskSnapshot
    events: tuple[PlanningTaskEvent, ...]
    review_decisions: tuple[PlanningTaskReviewDecisionRequest, ...] = ()
    accepted_decision_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_record(self) -> "_PersistedPlanningTaskRecord":
        task_id = self.snapshot.task_id
        expected_sequences = tuple(range(1, self.snapshot.event_count + 1))
        if tuple(event.sequence for event in self.events) != expected_sequences:
            raise ValueError("persisted task events must be contiguous")
        if any(event.task_id != task_id for event in self.events):
            raise ValueError("persisted task events must belong to the snapshot task")
        if not self.events or self.events[-1].task_status != self.snapshot.status:
            raise ValueError("persisted final event status must match the task snapshot")
        decision_ids = tuple(decision.decision_id for decision in self.review_decisions)
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("persisted review decision ids must be unique")
        if (
            self.accepted_decision_id is not None
            and self.accepted_decision_id not in decision_ids
        ):
            raise ValueError("accepted decision id must reference a persisted review decision")
        return self


class InMemoryPlanningTaskStore:
    """Process-local snapshots and replayable events for isolated tests and injection."""

    def __init__(self) -> None:
        self._snapshots: dict[str, PlanningTaskSnapshot] = {}
        self._events: dict[str, list[PlanningTaskEvent]] = {}
        self._review_decisions: dict[tuple[str, str], PlanningTaskReviewDecisionRequest] = {}
        self._task_decision_ids: dict[str, str] = {}
        self._condition = asyncio.Condition()

    async def create(self, submission: PlanningTaskSubmission) -> PlanningTaskSnapshot:
        async with self._condition:
            self._ensure_ready_locked()
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
            try:
                self._persist_task_locked(submission.task_id)
            except Exception:
                self._snapshots.pop(submission.task_id, None)
                self._events.pop(submission.task_id, None)
                raise
            self._condition.notify_all()
            return snapshot

    async def get(self, task_id: str) -> PlanningTaskSnapshot:
        async with self._condition:
            self._ensure_ready_locked()
            return self._require_snapshot(task_id)

    async def events_after(
        self,
        task_id: str,
        sequence: int,
    ) -> tuple[PlanningTaskEvent, ...]:
        async with self._condition:
            self._ensure_ready_locked()
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
            self._ensure_ready_locked()
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
        node: StatefulPlanningNodeName | ProductPlanningNodeName,
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
            self._ensure_ready_locked()
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
                if revision.operation == PlanRevisionOperation.REPLACE_ACTIVITY:
                    if not isinstance(previous.result, ProductPlanningSnapshot):
                        raise PlanningTaskReviewConflictError(
                            "revision-replacement-not-supported",
                            "当前任务结果不支持活动候选替换。",
                        )
                    target_items = {item.item_id: item for day in target_days for item in day.items}
                    specialists = previous.result.state.specialists
                    explore_branch = (
                        next(
                            (
                                item
                                for item in specialists.branches
                                if item.specialist == SpecialistName.EXPLORE
                            ),
                            None,
                        )
                        if specialists is not None
                        else None
                    )
                    observations = (
                        explore_branch.explore_result.observations
                        if explore_branch is not None and explore_branch.explore_result is not None
                        else ()
                    )
                    observed_by_id = {
                        item.candidate.candidate_id: item.candidate for item in observations
                    }
                    recovery = previous.result.state.weather_indoor_recovery
                    if recovery is not None:
                        observed_by_id.update(
                            {
                                item.candidate.candidate_id: item.candidate
                                for item in recovery.observations
                            }
                        )
                    scheduled_candidate_ids = {
                        item.candidate_id
                        for day in current_version.plan.days
                        for item in day.items
                        if item.candidate_id is not None
                    }
                    replacement_is_invalid = False
                    for pair in revision.replacement_pairs:
                        target_item = target_items.get(pair.replaced_item_id)
                        replacement = observed_by_id.get(pair.replacement_candidate_id)
                        if (
                            target_item is None
                            or target_item.kind != ActivityKind.ATTRACTION
                            or target_item.candidate_id is None
                            or replacement is None
                            or is_meal_candidate(replacement)
                            or replacement.candidate_id in scheduled_candidate_ids
                            or replacement.city != current_version.plan.destination_city
                        ):
                            replacement_is_invalid = True
                            break
                    if replacement_is_invalid:
                        raise PlanningTaskReviewConflictError(
                            "revision-replacement-not-eligible",
                            "每个替换候选都必须来自已记录的数据来源结果, "
                            "且不能是餐饮或已排入行程的地点。",
                        )

            self._review_decisions[key] = decision
            self._task_decision_ids[task_id] = decision.decision_id
            try:
                self._append_locked(
                    task_id,
                    kind=PlanningTaskEventKind.TASK_REVIEW_SUBMITTED,
                    status=PlanningTaskStatus.RUNNING,
                    message="审核决定已接收, 正在恢复原 LangGraph checkpoint。",
                    allowed_from={PlanningTaskStatus.AWAITING_INPUT},
                    review_id=decision.review_id,
                    review_action=decision.action,
                )
            except Exception:
                self._review_decisions.pop(key, None)
                self._task_decision_ids.pop(task_id, None)
                raise
            self._condition.notify_all()
            return False

    async def await_input(
        self,
        task_id: str,
        *,
        result: object,
        review_id: str,
    ) -> PlanningTaskSnapshot:
        snapshot = PLANNING_RESULT_ADAPTER.validate_python(result)
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
        snapshot = PLANNING_RESULT_ADAPTER.validate_python(result)
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

    async def await_input_after_revision(
        self,
        task_id: str,
        *,
        result: object,
        review_id: str,
        review_decision: PlanningTaskReviewDecisionRequest,
    ) -> PlanningTaskSnapshot:
        snapshot = PLANNING_RESULT_ADAPTER.validate_python(result)
        async with self._condition:
            self._ensure_ready_locked()
            current = self._require_snapshot(task_id)
            if not current.plan_versions:
                raise PlanningTaskTransitionError("revision completion requires a plan version")
            if review_decision.action != HumanReviewAction.REQUEST_REVISION:
                raise PlanningTaskTransitionError("only a revision can return to review")
            plan_versions = (
                *current.plan_versions,
                build_revised_plan_version(
                    snapshot,
                    current.plan_versions[-1],
                    created_at=utc_now(),
                ),
            )
            review_outcome = build_review_outcome(
                snapshot,
                review_decision,
                plan_versions,
            )
            accepted_decision_id = self._task_decision_ids.pop(task_id, None)
            if accepted_decision_id != review_decision.decision_id:
                if accepted_decision_id is not None:
                    self._task_decision_ids[task_id] = accepted_decision_id
                raise PlanningTaskTransitionError(
                    "completed revision does not match the accepted review decision"
                )
            try:
                updated = self._append_locked(
                    task_id,
                    kind=PlanningTaskEventKind.TASK_AWAITING_INPUT,
                    status=PlanningTaskStatus.AWAITING_INPUT,
                    message="修改版已生成, 正在等待用户继续审核。",
                    allowed_from={PlanningTaskStatus.RUNNING},
                    result=snapshot,
                    review_id=review_id,
                    plan_versions=plan_versions,
                    review_outcome=review_outcome,
                )
            except Exception:
                self._task_decision_ids[task_id] = review_decision.decision_id
                raise
            self._condition.notify_all()
            return updated

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
        node: StatefulPlanningNodeName | ProductPlanningNodeName | None = None,
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
            self._ensure_ready_locked()
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
        node: StatefulPlanningNodeName | ProductPlanningNodeName | None = None,
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
        try:
            self._persist_task_locked(task_id)
        except Exception:
            self._events[task_id].pop()
            self._snapshots[task_id] = previous
            raise
        return snapshot

    def _ensure_ready_locked(self) -> None:
        """Load durable state before the first operation; in-memory stores are always ready."""

    def _persist_task_locked(self, task_id: str) -> None:
        """Commit one task atomically; in-memory stores intentionally do nothing."""
        del task_id

    def _require_snapshot(self, task_id: str) -> PlanningTaskSnapshot:
        try:
            return self._snapshots[task_id]
        except KeyError as error:
            raise PlanningTaskNotFoundError(task_id) from error

    @staticmethod
    def _event_id(task_id: str, sequence: int) -> str:
        return f"{task_id}-event-{sequence:06d}"


class SQLitePlanningTaskStore(InMemoryPlanningTaskStore):
    """Single-process task ledger persisted as one validated JSON record per task."""

    def __init__(self, database_path: Path) -> None:
        super().__init__()
        self._database_path = database_path
        self._loaded = False

    def _ensure_ready_locked(self) -> None:
        if self._loaded:
            return
        try:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS planning_task_records (
                        task_id TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                rows = connection.execute(
                    "SELECT task_id, payload_json FROM planning_task_records ORDER BY task_id"
                ).fetchall()
            for stored_task_id, payload_json in rows:
                record = _PersistedPlanningTaskRecord.model_validate_json(payload_json)
                task_id = record.snapshot.task_id
                if stored_task_id != task_id or task_id in self._snapshots:
                    raise ValueError("persisted task row id does not match its payload")
                self._snapshots[task_id] = record.snapshot
                self._events[task_id] = list(record.events)
                for decision in record.review_decisions:
                    self._review_decisions[(task_id, decision.decision_id)] = decision
                if record.accepted_decision_id is not None:
                    self._task_decision_ids[task_id] = record.accepted_decision_id
            self._loaded = True
            self._mark_interrupted_tasks_failed_locked()
        except PlanningTaskPersistenceError:
            self._clear_loaded_state()
            raise
        except Exception as error:
            self._clear_loaded_state()
            raise PlanningTaskPersistenceError(
                "planning task ledger could not be loaded safely"
            ) from error

    def _persist_task_locked(self, task_id: str) -> None:
        snapshot = self._require_snapshot(task_id)
        decisions = tuple(
            decision
            for (decision_task_id, _), decision in sorted(self._review_decisions.items())
            if decision_task_id == task_id
        )
        record = _PersistedPlanningTaskRecord(
            snapshot=snapshot,
            events=tuple(self._events[task_id]),
            review_decisions=decisions,
            accepted_decision_id=self._task_decision_ids.get(task_id),
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO planning_task_records (task_id, payload_json)
                    VALUES (?, ?)
                    ON CONFLICT(task_id) DO UPDATE
                    SET payload_json = excluded.payload_json
                    """,
                    (task_id, record.model_dump_json()),
                )
                connection.commit()
        except Exception as error:
            raise PlanningTaskPersistenceError(
                f"planning task {task_id} could not be persisted"
            ) from error

    def _mark_interrupted_tasks_failed_locked(self) -> None:
        interrupted = tuple(
            task_id
            for task_id, snapshot in self._snapshots.items()
            if snapshot.status in {PlanningTaskStatus.QUEUED, PlanningTaskStatus.RUNNING}
        )
        for task_id in interrupted:
            failure = PlanningTaskFailure(
                error_code="planning-task-interrupted",
                category="workflow",
                retryable=True,
                user_message=(
                    "服务重启中断了正在执行的规划。为避免重复调用模型或旅行数据服务, "
                    "本任务已安全停止; 请重新创建任务。"
                ),
            )
            self._append_locked(
                task_id,
                kind=PlanningTaskEventKind.TASK_FAILED,
                status=PlanningTaskStatus.FAILED,
                message=failure.user_message,
                allowed_from={PlanningTaskStatus.QUEUED, PlanningTaskStatus.RUNNING},
                failure=failure,
                error_code=failure.error_code,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _clear_loaded_state(self) -> None:
        self._snapshots.clear()
        self._events.clear()
        self._review_decisions.clear()
        self._task_decision_ids.clear()
        self._loaded = False
