import asyncio
from pathlib import Path

from app.core.config import Settings
from app.planning.stateful_contracts import StatefulPlanningSnapshot
from app.planning.vertical_slice import VerticalSliceProtocolError
from app.tasks import (
    PlanningTaskService,
    PlanningTaskStatus,
    PlanningTaskSubmission,
    StatefulGraphPlanningTaskExecutor,
)
from tests.test_planning_task_api import build_fixture_payload


async def wait_for_event_count(
    service: PlanningTaskService,
    task_id: str,
    count: int,
) -> None:
    for _ in range(100):
        if (await service.get(task_id)).event_count >= count:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"task {task_id} did not reach {count} events")


class NeverCompletesExecutor:
    async def execute(
        self,
        submission: PlanningTaskSubmission,
        emit_progress,
    ) -> StatefulPlanningSnapshot:
        del submission, emit_progress
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_task_timeout_becomes_a_typed_retryable_failure() -> None:
    async def exercise() -> None:
        service = PlanningTaskService(
            NeverCompletesExecutor(),
            heartbeat_seconds=0.005,
            timeout_seconds=0.02,
        )
        accepted = await service.submit(build_fixture_payload())
        events = [
            event async for event in service.stream_events(accepted.task_id) if event is not None
        ]
        snapshot = await service.get(accepted.task_id)

        assert [event.kind.value for event in events] == [
            "task_created",
            "task_started",
            "task_failed",
        ]
        assert snapshot.status == PlanningTaskStatus.FAILED
        assert snapshot.failure is not None
        assert snapshot.failure.error_code == "planning-task-timeout"
        assert snapshot.failure.retryable is True

    asyncio.run(exercise())


def test_idle_stream_emits_heartbeat_without_fabricating_progress() -> None:
    async def exercise() -> None:
        release = asyncio.Event()

        class BlockingExecutor:
            async def execute(
                self,
                submission: PlanningTaskSubmission,
                emit_progress,
            ) -> StatefulPlanningSnapshot:
                del submission, emit_progress
                await release.wait()
                raise VerticalSliceProtocolError("controlled stop")

        service = PlanningTaskService(
            BlockingExecutor(),
            heartbeat_seconds=0.005,
            timeout_seconds=1,
        )
        accepted = await service.submit(build_fixture_payload())
        await wait_for_event_count(service, accepted.task_id, 2)
        stream = service.stream_events(accepted.task_id, after_sequence=2)

        assert await anext(stream) is None
        await stream.aclose()
        assert (await service.get(accepted.task_id)).event_count == 2

        release.set()
        await wait_for_event_count(service, accepted.task_id, 3)
        assert (await service.get(accepted.task_id)).status == PlanningTaskStatus.FAILED

    asyncio.run(exercise())


def test_live_mode_is_explicitly_disabled_by_default(tmp_path: Path) -> None:
    async def exercise() -> None:
        settings = Settings(
            environment="test",
            planning_checkpoint_dir=tmp_path,
            planning_live_enabled=False,
        )
        service = PlanningTaskService(
            StatefulGraphPlanningTaskExecutor(settings),
            heartbeat_seconds=0.005,
            timeout_seconds=1,
        )
        payload = build_fixture_payload().model_copy(update={"data_mode": "live"})
        accepted = await service.submit(payload)
        _ = [event async for event in service.stream_events(accepted.task_id)]
        snapshot = await service.get(accepted.task_id)

        assert snapshot.status == PlanningTaskStatus.FAILED
        assert snapshot.failure is not None
        assert snapshot.failure.error_code == "planning-configuration-error"
        assert snapshot.failure.category.value == "configuration"
        assert not list(tmp_path.glob("*.sqlite"))

    asyncio.run(exercise())
