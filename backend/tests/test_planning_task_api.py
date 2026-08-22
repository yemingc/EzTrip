import asyncio
import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from app.core.config import Settings
from app.domain.request import ConstraintSet, TripRequest
from app.evaluation import load_vertical_slice_suite
from app.main import create_app
from app.planning.stateful_contracts import StatefulPlanningSnapshot
from app.planning.vertical_slice import VerticalSliceProtocolError
from app.tasks import (
    PlanningTaskCreateRequest,
    PlanningTaskService,
    PlanningTaskSubmission,
    StatefulGraphPlanningTaskExecutor,
)
from scripts.export_planning_task_schemas import (
    OUTPUT_PATH,
    build_planning_task_schema_bundle,
)


def build_fixture_payload() -> PlanningTaskCreateRequest:
    case = load_vertical_slice_suite().cases[0]
    request_payload = case.request.model_dump(mode="python")
    request_payload.update(
        {
            "request_id": "api-fixture-beijing-two-day",
            "raw_text": "两位成年人去北京玩两天, 必须去故宫和天坛公园。",
            "end_date": case.request.start_date.replace(day=3),
            "constraints": ConstraintSet(
                items=tuple(
                    item for item in case.request.constraints.items if item.value != "首都博物馆"
                )
            ),
        }
    )
    request = TripRequest.model_validate(request_payload)
    return PlanningTaskCreateRequest(
        request=request,
        cost_items=case.cost_items[:4],
        data_mode="fixture",
    )


def parse_sse_events(body: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


async def request_until_awaiting_input(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        planning_checkpoint_dir=tmp_path,
        planning_sse_heartbeat_seconds=0.01,
        planning_task_timeout_seconds=10,
    )
    service = PlanningTaskService(
        StatefulGraphPlanningTaskExecutor(settings),
        heartbeat_seconds=0.01,
        timeout_seconds=10,
    )
    app = create_app(settings=settings, planning_task_service=service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_response = await client.post(
            "/api/planning-tasks",
            json=build_fixture_payload().model_dump(mode="json"),
        )
        assert create_response.status_code == 202
        accepted = create_response.json()
        task_id = accepted["task_id"]
        assert accepted["status"] == "queued"
        assert accepted["task_url"] == f"/api/planning-tasks/{task_id}"

        stream_response = await client.get(accepted["events_url"])
        assert stream_response.status_code == 200
        assert stream_response.headers["content-type"].startswith("text/event-stream")
        assert stream_response.headers["cache-control"] == "no-cache"
        events = parse_sse_events(stream_response.text)
        assert [event["sequence"] for event in events] == [1, 2, 3, 4, 5]
        assert [event["kind"] for event in events] == [
            "task_created",
            "task_started",
            "graph_node_completed",
            "graph_node_completed",
            "task_awaiting_input",
        ]
        assert [event.get("node") for event in events[2:4]] == [
            "run_vertical_slice",
            "prepare_human_review",
        ]
        assert [event.get("state_status") for event in events[2:4]] == [
            "plan_ready",
            "awaiting_human_review",
        ]

        snapshot_response = await client.get(accepted["task_url"])
        assert snapshot_response.status_code == 200
        snapshot = snapshot_response.json()
        assert snapshot["status"] == "awaiting_input"
        assert snapshot["event_count"] == 5
        assert snapshot["result"]["state"]["status"] == "awaiting_human_review"
        assert snapshot["result"]["state"]["review_request"]["review_id"] == events[-1]["review_id"]

        replay_response = await client.get(
            accepted["events_url"],
            headers={"Last-Event-ID": str(events[2]["event_id"])},
        )
        replayed = parse_sse_events(replay_response.text)
        assert [event["sequence"] for event in replayed] == [4, 5]

        mismatch_response = await client.get(
            f"{accepted['events_url']}?after=2",
            headers={"Last-Event-ID": str(events[2]["event_id"])},
        )
        assert mismatch_response.status_code == 400
        assert mismatch_response.json()["detail"]["error_code"] == "invalid-event-cursor"

        missing_response = await client.get("/api/planning-tasks/planning-task-missing")
        assert missing_response.status_code == 404
        assert missing_response.json()["detail"]["error_code"] == "planning-task-not-found"


def test_real_graph_task_api_and_sse_replay(tmp_path: Path) -> None:
    asyncio.run(request_until_awaiting_input(tmp_path))


class FailingExecutor:
    async def execute(
        self,
        submission: PlanningTaskSubmission,
        emit_progress,
    ) -> StatefulPlanningSnapshot:
        del submission, emit_progress
        raise VerticalSliceProtocolError("raw-secret-user-input-must-not-leak")


async def request_failed_task() -> None:
    service = PlanningTaskService(FailingExecutor(), heartbeat_seconds=0.01, timeout_seconds=1)
    app = create_app(planning_task_service=service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/planning-tasks",
            json=build_fixture_payload().model_dump(mode="json"),
        )
        accepted = response.json()
        events_response = await client.get(accepted["events_url"])
        events = parse_sse_events(events_response.text)
        assert [event["kind"] for event in events] == [
            "task_created",
            "task_started",
            "task_failed",
        ]
        assert events[-1]["error_code"] == "planning-workflow-error"
        assert "raw-secret" not in events_response.text

        snapshot_response = await client.get(accepted["task_url"])
        assert snapshot_response.json()["failure"] == {
            "schema_version": "1.0",
            "error_code": "planning-workflow-error",
            "category": "workflow",
            "retryable": False,
            "user_message": "当前请求无法生成满足工作流契约的完整行程草案。",
        }


def test_task_failure_is_typed_and_sanitized() -> None:
    asyncio.run(request_failed_task())


def test_planning_task_schema_bundle_and_openapi_do_not_drift() -> None:
    committed = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    generated = build_planning_task_schema_bundle()

    assert committed == generated
    for schema in committed["models"].values():
        Draft202012Validator.check_schema(schema)

    openapi = create_app().openapi()
    assert set(openapi["paths"]) >= {
        "/api/planning-tasks",
        "/api/planning-tasks/{task_id}",
        "/api/planning-tasks/{task_id}/events",
    }
    assert openapi["paths"]["/api/planning-tasks"]["post"]["responses"]["202"]
