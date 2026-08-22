import asyncio

from app.planning.stateful_contracts import PlanningThreadStatus, StatefulPlanningNodeName
from app.tasks.contracts import (
    PlanningTaskEvent,
    PlanningTaskEventKind,
    PlanningTaskFailure,
    PlanningTaskSnapshot,
    PlanningTaskStatus,
    PlanningTaskSubmission,
    utc_now,
)


class PlanningTaskNotFoundError(KeyError):
    """Raised when a task id is not present in the process-local task store."""


class PlanningTaskTransitionError(RuntimeError):
    """Raised when task state and event transitions diverge."""


class InMemoryPlanningTaskStore:
    """Process-local snapshots and replayable events for the first API increment."""

    def __init__(self) -> None:
        self._snapshots: dict[str, PlanningTaskSnapshot] = {}
        self._events: dict[str, list[PlanningTaskEvent]] = {}
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

    async def await_input(
        self,
        task_id: str,
        *,
        result: object,
        review_id: str,
    ) -> PlanningTaskSnapshot:
        from app.planning.stateful_contracts import StatefulPlanningSnapshot

        snapshot = StatefulPlanningSnapshot.model_validate(result)
        return await self._append(
            task_id,
            kind=PlanningTaskEventKind.TASK_AWAITING_INPUT,
            status=PlanningTaskStatus.AWAITING_INPUT,
            message="规划草案已生成, 正在等待用户审核。",
            allowed_from={PlanningTaskStatus.RUNNING},
            result=snapshot,
            review_id=review_id,
        )

    async def succeed(
        self,
        task_id: str,
        *,
        result: object,
    ) -> PlanningTaskSnapshot:
        from app.planning.stateful_contracts import StatefulPlanningSnapshot

        snapshot = StatefulPlanningSnapshot.model_validate(result)
        return await self._append(
            task_id,
            kind=PlanningTaskEventKind.TASK_SUCCEEDED,
            status=PlanningTaskStatus.SUCCEEDED,
            message="规划工作流已完成。",
            allowed_from={PlanningTaskStatus.RUNNING},
            result=snapshot,
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
        error_code: str | None = None,
        result: object | None = None,
        failure: PlanningTaskFailure | None = None,
    ) -> PlanningTaskSnapshot:
        async with self._condition:
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
                }
            )
            snapshot = PlanningTaskSnapshot.model_validate(payload)
            self._events[task_id].append(event)
            self._snapshots[task_id] = snapshot
            self._condition.notify_all()
            return snapshot

    def _require_snapshot(self, task_id: str) -> PlanningTaskSnapshot:
        try:
            return self._snapshots[task_id]
        except KeyError as error:
            raise PlanningTaskNotFoundError(task_id) from error

    @staticmethod
    def _event_id(task_id: str, sequence: int) -> str:
        return f"{task_id}-event-{sequence:06d}"
