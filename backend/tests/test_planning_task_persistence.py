import asyncio
from pathlib import Path

from app.core.config import Settings
from app.planning.stateful_contracts import StatefulPlanningSnapshot
from app.tasks import (
    PlanningTaskReviewDecisionRequest,
    PlanningTaskService,
    PlanningTaskStatus,
    PlanningTaskSubmission,
    SQLitePlanningTaskStore,
    StatefulGraphPlanningTaskExecutor,
)
from tests.test_planning_task_api import build_fixture_payload


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        planning_checkpoint_dir=tmp_path / "checkpoints",
        planning_task_store_path=tmp_path / "planning-task-store.sqlite3",
        planning_sse_heartbeat_seconds=0.01,
        planning_task_timeout_seconds=10,
    )


def _service(settings: Settings) -> PlanningTaskService:
    return PlanningTaskService(
        StatefulGraphPlanningTaskExecutor(settings),
        store=SQLitePlanningTaskStore(settings.planning_task_store_path),
        heartbeat_seconds=0.01,
        timeout_seconds=10,
    )


def test_awaiting_review_and_terminal_task_survive_service_reconstruction(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        settings = _settings(tmp_path)
        first_service = _service(settings)
        accepted = await first_service.submit(build_fixture_payload())
        initial_events = [
            event
            async for event in first_service.stream_events(accepted.task_id)
            if event is not None
        ]
        awaiting = await first_service.get(accepted.task_id)
        assert awaiting.status == PlanningTaskStatus.AWAITING_INPUT
        assert settings.planning_task_store_path.exists()

        reconstructed_service = _service(settings)
        reconstructed = await reconstructed_service.get(accepted.task_id)
        assert reconstructed == awaiting
        assert await reconstructed_service.events_after(accepted.task_id, 0) == tuple(
            initial_events
        )

        assert reconstructed.result is not None
        review = reconstructed.result.state.review_request
        assert review is not None
        decision = PlanningTaskReviewDecisionRequest(
            decision_id="review-decision-persistent-001",
            review_id=review.review_id,
            action="approve_draft",
            reviewer_id="persistent-reviewer",
            comment="批准持久化恢复后的草案。",
        )
        review_accepted = await reconstructed_service.submit_review(accepted.task_id, decision)
        assert review_accepted.idempotent_replay is False
        resumed_events = [
            event
            async for event in reconstructed_service.stream_events(
                accepted.task_id,
                after_sequence=awaiting.event_count,
            )
            if event is not None
        ]
        assert [event.kind.value for event in resumed_events] == [
            "task_review_submitted",
            "graph_node_completed",
            "graph_node_completed",
            "task_succeeded",
        ]
        terminal = await reconstructed_service.get(accepted.task_id)
        assert terminal.status == PlanningTaskStatus.SUCCEEDED
        assert terminal.review_outcome is not None

        final_service = _service(settings)
        assert await final_service.get(accepted.task_id) == terminal
        assert await final_service.events_after(accepted.task_id, 0) == (
            *initial_events,
            *resumed_events,
        )
        replay = await final_service.submit_review(accepted.task_id, decision)
        assert replay.idempotent_replay is True
        assert (await final_service.get(accepted.task_id)).event_count == terminal.event_count

    asyncio.run(exercise())


class BlockingExecutor:
    async def execute(
        self,
        submission: PlanningTaskSubmission,
        emit_progress,
    ) -> StatefulPlanningSnapshot:
        del submission, emit_progress
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def _wait_until_running(service: PlanningTaskService, task_id: str) -> None:
    for _ in range(200):
        if (await service.get(task_id)).status == PlanningTaskStatus.RUNNING:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("planning task did not start")


def test_interrupted_running_task_becomes_actionable_failure_without_replay(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        settings = _settings(tmp_path)
        first_service = PlanningTaskService(
            BlockingExecutor(),
            store=SQLitePlanningTaskStore(settings.planning_task_store_path),
            heartbeat_seconds=0.01,
            timeout_seconds=10,
        )
        accepted = await first_service.submit(build_fixture_payload())
        await _wait_until_running(first_service, accepted.task_id)
        assert (await first_service.get(accepted.task_id)).event_count == 2
        await first_service.shutdown()

        reconstructed_service = PlanningTaskService(
            BlockingExecutor(),
            store=SQLitePlanningTaskStore(settings.planning_task_store_path),
            heartbeat_seconds=0.01,
            timeout_seconds=10,
        )
        interrupted = await reconstructed_service.get(accepted.task_id)
        assert interrupted.status == PlanningTaskStatus.FAILED
        assert interrupted.event_count == 3
        assert interrupted.failure is not None
        assert interrupted.failure.error_code == "planning-task-interrupted"
        assert interrupted.failure.retryable is True
        assert "避免重复调用模型或旅行数据服务" in interrupted.failure.user_message
        events = await reconstructed_service.events_after(accepted.task_id, 0)
        assert [event.kind.value for event in events] == [
            "task_created",
            "task_started",
            "task_failed",
        ]

    asyncio.run(exercise())
