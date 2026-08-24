import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol
from uuid import uuid4

from app.agents.plan_agent import PlanAgentConfigurationError, PlanAgentProtocolError
from app.agents.single_planner import (
    SinglePlannerConfigurationError,
    SinglePlannerProtocolError,
)
from app.destinations import (
    DestinationResolutionConfigurationError,
    DestinationSelectionError,
)
from app.planning.material_builder import PlanningMaterialProtocolError
from app.planning.minimal_graph import PlanningGraphProtocolError
from app.planning.plan_revision import PlanRevisionProtocolError
from app.planning.product_contracts import ProductPlanningProgress, ProductPlanningSnapshot
from app.planning.product_graph import ProductPlanningProtocolError
from app.planning.product_repair import ProductRepairProtocolError
from app.planning.repair_router import RepairRouterProtocolError
from app.planning.specialist_fanout import (
    SpecialistFanoutConfigurationError,
    SpecialistFanoutProtocolError,
)
from app.planning.stateful_contracts import (
    HumanReviewResume,
    PlanningThreadStatus,
    StatefulPlanningProgress,
    StatefulPlanningSnapshot,
)
from app.planning.stateful_graph import StatefulPlanningProtocolError
from app.planning.vertical_slice import VerticalSliceProtocolError
from app.providers.errors import ProviderRequestError
from app.tasks.contracts import (
    PlanningTaskAccepted,
    PlanningTaskCreateRequest,
    PlanningTaskEvent,
    PlanningTaskFailure,
    PlanningTaskFailureCategory,
    PlanningTaskReviewDecisionAccepted,
    PlanningTaskReviewDecisionRequest,
    PlanningTaskSnapshot,
    PlanningTaskStatus,
    PlanningTaskSubmission,
)
from app.tasks.store import (
    InMemoryPlanningTaskStore,
    PlanningTaskNotFoundError,
    PlanningTaskReviewConflictError,
)

PlanningResultSnapshot = StatefulPlanningSnapshot | ProductPlanningSnapshot
PlanningProgress = StatefulPlanningProgress | ProductPlanningProgress
PlanningProgressEmitter = Callable[[PlanningProgress], Awaitable[None]]


class PlanningTaskConfigurationError(RuntimeError):
    """Raised when a requested task mode is intentionally disabled or incomplete."""


class PlanningTaskExecutor(Protocol):
    async def execute(
        self,
        submission: PlanningTaskSubmission,
        emit_progress: PlanningProgressEmitter,
    ) -> PlanningResultSnapshot: ...

    async def resume(
        self,
        task_id: str,
        resume: HumanReviewResume,
        emit_progress: PlanningProgressEmitter,
    ) -> PlanningResultSnapshot: ...


class PlanningTaskService:
    def __init__(
        self,
        executor: PlanningTaskExecutor,
        *,
        store: InMemoryPlanningTaskStore | None = None,
        heartbeat_seconds: float = 15.0,
        timeout_seconds: float = 120.0,
    ) -> None:
        if heartbeat_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("task heartbeat and timeout must be positive")
        self._executor = executor
        self._store = store or InMemoryPlanningTaskStore()
        self._heartbeat_seconds = heartbeat_seconds
        self._timeout_seconds = timeout_seconds
        self._workers: set[asyncio.Task[None]] = set()

    async def submit(self, request: PlanningTaskCreateRequest) -> PlanningTaskAccepted:
        task_id = f"planning-task-{uuid4().hex}"
        submission = PlanningTaskSubmission(
            task_id=task_id,
            **request.model_dump(mode="python"),
        )
        await self._store.create(submission)
        worker = asyncio.create_task(self._run(submission), name=task_id)
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)
        return PlanningTaskAccepted(
            task_id=task_id,
            request_id=request.request.request_id,
            task_url=f"/api/planning-tasks/{task_id}",
            events_url=f"/api/planning-tasks/{task_id}/events",
        )

    async def get(self, task_id: str) -> PlanningTaskSnapshot:
        return await self._store.get(task_id)

    async def submit_review(
        self,
        task_id: str,
        decision: PlanningTaskReviewDecisionRequest,
    ) -> PlanningTaskReviewDecisionAccepted:
        idempotent_replay = await self._store.submit_review(task_id, decision)
        if not idempotent_replay:
            worker = asyncio.create_task(
                self._resume(task_id, decision),
                name=f"{task_id}-review-{decision.decision_id}",
            )
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)
        return PlanningTaskReviewDecisionAccepted(
            decision_id=decision.decision_id,
            task_id=task_id,
            review_id=decision.review_id,
            action=decision.action,
            idempotent_replay=idempotent_replay,
            task_url=f"/api/planning-tasks/{task_id}",
            events_url=f"/api/planning-tasks/{task_id}/events",
        )

    async def events_after(
        self,
        task_id: str,
        sequence: int,
    ) -> tuple[PlanningTaskEvent, ...]:
        if sequence < 0:
            raise ValueError("event sequence cannot be negative")
        snapshot = await self._store.get(task_id)
        if sequence > snapshot.event_count:
            raise ValueError("event cursor is ahead of the task event log")
        return await self._store.events_after(task_id, sequence)

    async def stream_events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[PlanningTaskEvent | None]:
        await self.events_after(task_id, after_sequence)
        cursor = after_sequence
        terminal = {
            PlanningTaskStatus.AWAITING_INPUT,
            PlanningTaskStatus.SUCCEEDED,
            PlanningTaskStatus.FAILED,
        }
        while True:
            events = await self._store.events_after(task_id, cursor)
            for event in events:
                cursor = event.sequence
                yield event
            snapshot = await self._store.get(task_id)
            if snapshot.status in terminal and cursor >= snapshot.event_count:
                return
            events = await self._store.wait_for_events(
                task_id,
                cursor,
                timeout_seconds=self._heartbeat_seconds,
            )
            if not events:
                snapshot = await self._store.get(task_id)
                if snapshot.status in terminal and cursor >= snapshot.event_count:
                    return
                yield None

    @staticmethod
    def parse_event_cursor(task_id: str, event_id: str | None) -> int:
        if event_id is None or not event_id.strip():
            return 0
        prefix = f"{task_id}-event-"
        if not event_id.startswith(prefix):
            raise ValueError("Last-Event-ID does not belong to this task")
        suffix = event_id.removeprefix(prefix)
        if len(suffix) != 6 or not suffix.isdigit():
            raise ValueError("Last-Event-ID has an invalid sequence")
        return int(suffix)

    async def _run(self, submission: PlanningTaskSubmission) -> None:
        await self._store.start(submission.task_id)

        async def emit(progress: PlanningProgress) -> None:
            await self._store.record_node(
                submission.task_id,
                node=progress.node,
                state_status=progress.state_status,
                message=progress.event.detail,
            )

        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await self._executor.execute(submission, emit)
            if result.state.status == PlanningThreadStatus.AWAITING_HUMAN_REVIEW:
                review = result.state.review_request
                if review is None:
                    raise ProductPlanningProtocolError(
                        "awaiting review snapshot contains no review request"
                    )
                await self._store.await_input(
                    submission.task_id,
                    result=result,
                    review_id=review.review_id,
                )
            else:
                await self._store.succeed(submission.task_id, result=result)
        except Exception as error:
            await self._store.fail(
                submission.task_id,
                failure=self._safe_failure(error),
            )

    async def _resume(
        self,
        task_id: str,
        decision: PlanningTaskReviewDecisionRequest,
    ) -> None:
        async def emit(progress: PlanningProgress) -> None:
            await self._store.record_node(
                task_id,
                node=progress.node,
                state_status=progress.state_status,
                message=progress.event.detail,
            )

        resume = HumanReviewResume(
            review_id=decision.review_id,
            action=decision.action,
            reviewer_id=decision.reviewer_id,
            comment=decision.comment,
            revision_request=decision.revision_request,
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await self._executor.resume(task_id, resume, emit)
            await self._store.succeed(
                task_id,
                result=result,
                review_decision=decision,
            )
        except Exception as error:
            await self._store.fail(
                task_id,
                failure=self._safe_failure(error),
            )

    @staticmethod
    def _safe_failure(error: Exception) -> PlanningTaskFailure:
        if isinstance(error, DestinationSelectionError):
            return PlanningTaskFailure(
                error_code=error.error_code,
                category=PlanningTaskFailureCategory.PROVIDER,
                retryable=False,
                user_message=error.user_message,
            )
        if isinstance(error, TimeoutError):
            return PlanningTaskFailure(
                error_code="planning-task-timeout",
                category=PlanningTaskFailureCategory.WORKFLOW,
                retryable=True,
                user_message="规划任务执行超时, 请稍后重试。",
            )
        if isinstance(error, ProviderRequestError):
            category = error.failure.category.value.replace("_", "-")
            return PlanningTaskFailure(
                error_code=f"provider-{category}",
                category=PlanningTaskFailureCategory.PROVIDER,
                retryable=error.failure.retryable,
                user_message="旅行数据服务暂时无法完成请求, 请检查配置或稍后重试。",
            )
        if isinstance(
            error,
            (
                PlanningTaskConfigurationError,
                DestinationResolutionConfigurationError,
                SinglePlannerConfigurationError,
                PlanAgentConfigurationError,
                SpecialistFanoutConfigurationError,
            ),
        ):
            return PlanningTaskFailure(
                error_code="planning-configuration-error",
                category=PlanningTaskFailureCategory.CONFIGURATION,
                retryable=False,
                user_message="当前规划模式尚未正确配置。",
            )
        if isinstance(
            error,
            (
                PlanningGraphProtocolError,
                PlanningMaterialProtocolError,
                PlanRevisionProtocolError,
                PlanAgentProtocolError,
                ProductPlanningProtocolError,
                ProductRepairProtocolError,
                RepairRouterProtocolError,
                SinglePlannerProtocolError,
                SpecialistFanoutProtocolError,
                StatefulPlanningProtocolError,
                VerticalSliceProtocolError,
            ),
        ):
            return PlanningTaskFailure(
                error_code="planning-workflow-error",
                category=PlanningTaskFailureCategory.WORKFLOW,
                retryable=False,
                user_message="当前请求无法生成满足工作流契约的完整行程草案。",
            )
        return PlanningTaskFailure(
            error_code="planning-internal-error",
            category=PlanningTaskFailureCategory.INTERNAL,
            retryable=False,
            user_message="规划任务发生内部错误。",
        )


__all__ = [
    "PlanningTaskConfigurationError",
    "PlanningTaskExecutor",
    "PlanningTaskNotFoundError",
    "PlanningTaskReviewConflictError",
    "PlanningTaskService",
]
