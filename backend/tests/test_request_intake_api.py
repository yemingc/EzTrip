import asyncio
from datetime import date

from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


def payload(*, data_mode: str = "fixture") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "raw_text": "带一个孩子去北京看科技馆, 不要寺庙, 预算5000, 轻松玩两天",
        "reference_date": date(2026, 8, 24).isoformat(),
        "data_mode": data_mode,
        "form": {
            "origin_city": "上海",
            "destination_city": "北京",
            "start_date": "2026-09-10",
            "trip_days": 2,
            "adults": 2,
            "children": 0,
            "seniors": 0,
            "rooms": 1,
            "budget_limit": "3000",
            "pace": "standard",
        },
    }


def test_fixture_request_intake_requires_explicit_confirmation_before_planning() -> None:
    async def exercise() -> None:
        settings = Settings(_env_file=None, environment="test")
        app = create_app(settings=settings)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/request-intakes", json=payload())
            assert response.status_code == 200
            draft = response.json()
            assert draft["model_call_count"] == 0
            assert draft["proposed_fields"]["children"] == 1
            assert "task_id" not in draft

            unknown_task = await client.get(f"/api/planning-tasks/{draft['draft_id']}")
            assert unknown_task.status_code == 404

            confirmed_response = await client.post(
                f"/api/request-intakes/{draft['draft_id']}/confirm",
                json={
                    "schema_version": "1.0",
                    "selection": "proposal",
                    "selected_destination_adcode": "110000",
                },
            )
            assert confirmed_response.status_code == 200
            confirmed = confirmed_response.json()
            assert confirmed["request"]["party"] == {
                "adults": 1,
                "children": 1,
                "seniors": 0,
                "rooms": 1,
            }
            assert confirmed["request"]["travel_styles"] == ["科技"]
            assert confirmed["request"]["constraints"]["items"][0]["confirmed"] is True

    asyncio.run(exercise())


def test_live_intake_fails_closed_before_planning_when_model_is_not_configured() -> None:
    async def exercise() -> None:
        settings = Settings(
            _env_file=None,
            environment="test",
            deepseek_api_key=None,
            langsmith_tracing=False,
        )
        app = create_app(settings=settings)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/request-intakes",
                json=payload(data_mode="live"),
            )
        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "request-intake-configuration"

    asyncio.run(exercise())


def test_request_intake_openapi_paths_are_public() -> None:
    paths = create_app(settings=Settings(_env_file=None, environment="test")).openapi()["paths"]
    assert "/api/request-intakes" in paths
    assert "/api/request-intakes/{draft_id}/confirm" in paths
