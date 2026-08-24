import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from app.core.config import Settings
from app.domain.request import ConstraintSet, TripRequest
from app.evaluation import load_vertical_slice_suite
from app.main import create_app
from app.planning.material_contracts import PlanningMaterialIssueCode
from app.planning.product_graph import ProductPlanningMaterialsBlockedError
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
                    item.model_copy(update={"value": "故宫博物院"})
                    if item.value == "故宫"
                    else item
                    for item in case.request.constraints.items
                    if item.value != "首都博物馆"
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


@pytest.mark.parametrize(
    ("destination", "days", "expected_pois"),
    [
        ("上海", 3, {"上海博物馆", "豫园"}),
        ("成都", 5, {"金沙遗址博物馆", "成都大熊猫繁育研究基地"}),
    ],
)
def test_fixture_product_flow_uses_dynamic_destination_and_trip_days(
    tmp_path: Path,
    destination: str,
    days: int,
    expected_pois: set[str],
) -> None:
    async def exercise() -> None:
        settings = Settings(
            _env_file=None,
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
        fixture = build_fixture_payload()
        request_payload = fixture.request.model_dump(mode="python")
        request_payload.update(
            destination_city=destination,
            destination_adcode=None,
            raw_text=f"规划一次{destination}{days}日游。",
            end_date=fixture.request.start_date + timedelta(days=days - 1),
            constraints=ConstraintSet(),
        )
        request = TripRequest.model_validate(request_payload)
        accepted = await service.submit(
            fixture.model_copy(
                update={
                    "request": request,
                    "selected_destination_adcode": None,
                }
            )
        )
        _ = [event async for event in service.stream_events(accepted.task_id)]
        snapshot = await service.get(accepted.task_id)
        assert snapshot.status.value == "awaiting_input"
        assert snapshot.result is not None
        state = snapshot.result.state
        assert state.plan is not None
        assert state.plan.destination_city in {"上海市", "成都市"}
        assert len(state.plan.days) == days
        assert {
            item.title
            for day in state.plan.days
            for item in day.items
            if item.candidate_id is not None
        } == expected_pois

    asyncio.run(exercise())


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
        assert [event["sequence"] for event in events] == list(range(1, 10))
        assert [event["kind"] for event in events] == [
            "task_created",
            "task_started",
            "graph_node_completed",
            "graph_node_completed",
            "graph_node_completed",
            "graph_node_completed",
            "graph_node_completed",
            "graph_node_completed",
            "task_awaiting_input",
        ]
        assert [event.get("node") for event in events[2:8]] == [
            "run_specialists",
            "build_materials",
            "run_plan_agent",
            "validate_hard_plan",
            "run_repair",
            "prepare_human_review",
        ]
        assert [event.get("state_status") for event in events[2:8]] == [
            "planning",
            "planning",
            "planning",
            "plan_ready",
            "plan_ready",
            "awaiting_human_review",
        ]

        snapshot_response = await client.get(accepted["task_url"])
        assert snapshot_response.status_code == 200
        snapshot = snapshot_response.json()
        assert snapshot["status"] == "awaiting_input"
        assert snapshot["event_count"] == 9
        assert snapshot["result"]["state"]["workflow_version"] == "product-planning-graph-v2"
        assert snapshot["result"]["state"]["status"] == "awaiting_human_review"
        assert snapshot["result"]["state"]["review_request"]["review_id"] == events[-1]["review_id"]
        product_state = snapshot["result"]["state"]
        assert product_state["specialists"]["status"] == "complete"
        assert [item["status"] for item in product_state["specialists"]["branches"]] == [
            "succeeded",
            "succeeded",
            "succeeded",
        ]
        assert product_state["materials"]["status"] == "ready"
        assert product_state["plan_agent"]["status"] == "planned"
        assert product_state["validation"]["validator_version"] == ("hard-trip-plan-validator-v1")
        assert product_state["validation"]["can_finalize"] is True
        assert product_state["repair"]["outcome"] == "repaired"
        assert product_state["repair"]["stop_reason"] == "finalizable"
        assert product_state["repair"]["total_model_call_count"] == 0
        assert product_state["repair"]["total_provider_call_count"] == 0
        assert len(product_state["repair"]["attempts"]) == 1
        repair_attempt = product_state["repair"]["attempts"][0]
        assert repair_attempt["repair_action"] == "replan_day"
        assert repair_attempt["executed_nodes"] == ["plan"]
        assert repair_attempt["reused_nodes"] == [
            "constraint",
            "explore",
            "stay",
            "weather",
            "route",
            "budget",
        ]
        assert repair_attempt["resolved_issue_codes"] == [
            "opening_hours.schedule_outside_verified_window"
        ]
        assert product_state["plan"]["weather_risks"][0]["risk_type"] == "rain"
        scheduled = {
            item["title"]: day["date"]
            for day in product_state["plan"]["days"]
            for item in day["items"]
        }
        assert scheduled["故宫博物院"] == product_state["plan"]["start_date"]
        assert scheduled["天坛公园"] == product_state["plan"]["end_date"]
        temple = next(
            item
            for day in product_state["plan"]["days"]
            for item in day["items"]
            if item["title"] == "天坛公园"
        )
        assert temple["start_at"].endswith("10:00:00+08:00")
        assert len(snapshot["plan_versions"]) == 1
        assert snapshot["plan_versions"][0]["version_number"] == 1
        assert set(snapshot["plan_versions"][0]["model_versions"]) == {
            "explore_query",
            "explore_selection",
            "stay_query",
            "stay_selection",
            "plan",
        }
        assert (
            "Repair Router 执行 1 次有界修复" in snapshot["plan_versions"][0]["change_summary"][1]
        )
        assert snapshot["review_outcome"] is None

        replay_response = await client.get(
            accepted["events_url"],
            headers={"Last-Event-ID": str(events[2]["event_id"])},
        )
        replayed = parse_sse_events(replay_response.text)
        assert [event["sequence"] for event in replayed] == [4, 5, 6, 7, 8, 9]

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


async def request_review_until_completed(tmp_path: Path) -> None:
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
        accepted = create_response.json()
        initial_events_response = await client.get(accepted["events_url"])
        initial_events = parse_sse_events(initial_events_response.text)
        review_id = str(initial_events[-1]["review_id"])
        decision = {
            "decision_id": "review-decision-api-001",
            "review_id": review_id,
            "action": "approve_draft",
            "reviewer_id": "reviewer-api-fixture",
            "comment": "批准当前草案。",
        }

        concurrent_responses = await asyncio.gather(
            client.post(
                f"{accepted['task_url']}/review-decisions",
                json=decision,
            ),
            client.post(
                f"{accepted['task_url']}/review-decisions",
                json=decision,
            ),
        )
        assert [response.status_code for response in concurrent_responses] == [202, 202]
        response_payloads = [response.json() for response in concurrent_responses]
        assert sorted(payload["idempotent_replay"] for payload in response_payloads) == [
            False,
            True,
        ]
        accepted_payload = next(
            payload for payload in response_payloads if not payload["idempotent_replay"]
        )
        assert accepted_payload == {
            "schema_version": "1.0",
            "decision_id": "review-decision-api-001",
            "task_id": accepted["task_id"],
            "review_id": review_id,
            "action": "approve_draft",
            "status": "running",
            "idempotent_replay": False,
            "task_url": accepted["task_url"],
            "events_url": accepted["events_url"],
        }

        conflicting_replay = await client.post(
            f"{accepted['task_url']}/review-decisions",
            json={**decision, "action": "cancel"},
        )
        assert conflicting_replay.status_code == 409
        assert (
            conflicting_replay.json()["detail"]["error_code"]
            == "review-decision-idempotency-conflict"
        )

        resumed_events_response = await client.get(f"{accepted['events_url']}?after=9")
        resumed_events = parse_sse_events(resumed_events_response.text)
        assert [event["sequence"] for event in resumed_events] == [10, 11, 12, 13]
        assert [event["kind"] for event in resumed_events] == [
            "task_review_submitted",
            "graph_node_completed",
            "graph_node_completed",
            "task_succeeded",
        ]
        assert resumed_events[0]["review_id"] == review_id
        assert resumed_events[0]["review_action"] == "approve_draft"
        assert [event.get("node") for event in resumed_events[1:3]] == [
            "human_review",
            "apply_review_decision",
        ]
        assert [event.get("state_status") for event in resumed_events[1:3]] == [
            "review_decided",
            "approved_draft",
        ]

        snapshot_response = await client.get(accepted["task_url"])
        snapshot = snapshot_response.json()
        assert snapshot["status"] == "succeeded"
        assert snapshot["event_count"] == 13
        assert snapshot["result"]["state"]["status"] == "approved_draft"
        assert len(snapshot["plan_versions"]) == 1
        version = snapshot["plan_versions"][0]
        outcome = snapshot["review_outcome"]
        assert outcome["decision_id"] == decision["decision_id"]
        assert outcome["resulting_state_status"] == "approved_draft"
        assert outcome["plan_diff"] == {
            "schema_version": "1.0",
            "from_version_id": version["version_id"],
            "to_version_id": version["version_id"],
            "plan_changed": False,
            "changed_dates": [],
            "added_item_ids": [],
            "removed_item_ids": [],
            "rescheduled_item_ids": [],
            "summary": ["用户批准现有草案, 审核恢复没有修改行程结构。"],
        }

        second_decision = await client.post(
            f"{accepted['task_url']}/review-decisions",
            json={**decision, "decision_id": "review-decision-api-002"},
        )
        assert second_decision.status_code == 409
        assert second_decision.json()["detail"]["error_code"] == "review-already-decided"


def test_review_resume_is_idempotent_and_streams_checkpoint_progress(tmp_path: Path) -> None:
    asyncio.run(request_review_until_completed(tmp_path))


async def request_structured_revision_until_v2(tmp_path: Path) -> None:
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
        accepted = create_response.json()
        _ = await client.get(accepted["events_url"])
        initial_snapshot = (await client.get(accepted["task_url"])).json()
        review_id = initial_snapshot["result"]["state"]["review_request"]["review_id"]
        version = initial_snapshot["plan_versions"][0]
        plan = version["plan"]
        target_day = plan["days"][1]
        revision_request = {
            "revision_id": "revision-api-day-two-later-v1",
            "base_version_id": version["version_id"],
            "base_plan_id": plan["plan_id"],
            "target_date": target_day["date"],
            "operation": "shift_day_later",
            "shift_minutes": 120,
            "target_item_ids": [item["item_id"] for item in target_day["items"]],
            "protected_item_ids": [
                item["item_id"]
                for day in plan["days"]
                if day["date"] != target_day["date"]
                for item in day["items"]
            ],
            "confirmed": True,
        }
        decision = {
            "decision_id": "review-decision-api-revision-v1",
            "review_id": review_id,
            "action": "request_revision",
            "reviewer_id": "reviewer-api-fixture",
            "comment": "将第二天整体延后两小时。",
            "revision_request": revision_request,
        }

        missing_revision = await client.post(
            f"{accepted['task_url']}/review-decisions",
            json={key: value for key, value in decision.items() if key != "revision_request"},
        )
        assert missing_revision.status_code == 422

        stale_revision = await client.post(
            f"{accepted['task_url']}/review-decisions",
            json={
                **decision,
                "decision_id": "review-decision-api-revision-stale",
                "revision_request": {
                    **revision_request,
                    "base_version_id": "plan-version-stale-v1",
                },
            },
        )
        assert stale_revision.status_code == 409
        assert stale_revision.json()["detail"]["error_code"] == ("revision-base-version-mismatch")

        scope_drift = await client.post(
            f"{accepted['task_url']}/review-decisions",
            json={
                **decision,
                "decision_id": "review-decision-api-revision-scope-drift",
                "revision_request": {
                    **revision_request,
                    "protected_item_ids": [],
                },
            },
        )
        assert scope_drift.status_code == 409
        assert scope_drift.json()["detail"]["error_code"] == "revision-scope-mismatch"

        decision_response = await client.post(
            f"{accepted['task_url']}/review-decisions",
            json=decision,
        )
        assert decision_response.status_code == 202

        resumed_response = await client.get(f"{accepted['events_url']}?after=9")
        resumed = parse_sse_events(resumed_response.text)
        assert [event["sequence"] for event in resumed] == [10, 11, 12, 13, 14]
        assert [event["kind"] for event in resumed] == [
            "task_review_submitted",
            "graph_node_completed",
            "graph_node_completed",
            "graph_node_completed",
            "task_succeeded",
        ]
        assert [event.get("node") for event in resumed[1:4]] == [
            "human_review",
            "apply_review_decision",
            "apply_plan_revision",
        ]
        assert [event.get("state_status") for event in resumed[1:4]] == [
            "review_decided",
            "revision_requested",
            "revision_applied",
        ]

        terminal = (await client.get(accepted["task_url"])).json()
        assert terminal["status"] == "succeeded"
        assert terminal["result"]["state"]["status"] == "revision_applied"
        assert terminal["result"]["state"]["revision_result"]["model_call_count"] == 0
        assert terminal["result"]["state"]["revision_result"]["provider_call_count"] == 0
        assert [item["version_number"] for item in terminal["plan_versions"]] == [1, 2]
        revised_version = terminal["plan_versions"][1]
        assert revised_version["based_on_version_id"] == version["version_id"]
        assert revised_version["changed_dates"] == [target_day["date"]]
        outcome = terminal["review_outcome"]
        assert outcome["plan_diff"]["from_version_id"] == version["version_id"]
        assert outcome["plan_diff"]["to_version_id"] == revised_version["version_id"]
        assert outcome["plan_diff"]["plan_changed"] is True
        assert outcome["plan_diff"]["changed_dates"] == [target_day["date"]]
        assert outcome["plan_diff"]["rescheduled_item_ids"] == revision_request["target_item_ids"]
        assert terminal["plan_versions"][0]["plan"] == plan
        assert revised_version["plan"]["days"][0] == plan["days"][0]


def test_structured_revision_creates_a_scoped_v2(tmp_path: Path) -> None:
    asyncio.run(request_structured_revision_until_v2(tmp_path))


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


class BlockedMaterialsExecutor:
    async def execute(
        self,
        submission: PlanningTaskSubmission,
        emit_progress,
    ) -> StatefulPlanningSnapshot:
        del submission, emit_progress
        raise ProductPlanningMaterialsBlockedError(
            (
                PlanningMaterialIssueCode.SPECIALIST_INCOMPLETE,
                PlanningMaterialIssueCode.STAY_ANCHOR_MISSING,
            )
        )


async def request_materials_blocked_task() -> None:
    service = PlanningTaskService(
        BlockedMaterialsExecutor(),
        heartbeat_seconds=0.01,
        timeout_seconds=1,
    )
    app = create_app(planning_task_service=service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/planning-tasks",
            json=build_fixture_payload().model_dump(mode="json"),
        )
        accepted = response.json()
        await client.get(accepted["events_url"])
        snapshot = (await client.get(accepted["task_url"])).json()

    assert snapshot["failure"] == {
        "schema_version": "1.0",
        "error_code": "planning-materials-blocked",
        "category": "workflow",
        "retryable": True,
        "user_message": ("当前城市未取得任何可核验的景点事实, 无法安全生成草案; 请稍后重试。"),
    }


def test_materials_blocked_failure_is_actionable() -> None:
    asyncio.run(request_materials_blocked_task())


def test_planning_task_schema_bundle_and_openapi_do_not_drift() -> None:
    committed = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    generated = build_planning_task_schema_bundle()

    assert committed == generated
    for schema in committed["models"].values():
        Draft202012Validator.check_schema(schema)

    openapi = create_app().openapi()
    assert set(openapi["paths"]) >= {
        "/api/destinations/resolve",
        "/api/planning-tasks",
        "/api/planning-tasks/{task_id}",
        "/api/planning-tasks/{task_id}/events",
        "/api/planning-tasks/{task_id}/review-decisions",
    }
    assert openapi["paths"]["/api/planning-tasks"]["post"]["responses"]["202"]
    assert openapi["paths"]["/api/planning-tasks/{task_id}/review-decisions"]["post"]["responses"][
        "202"
    ]
