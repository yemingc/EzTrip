import re
from pathlib import Path
from typing import Any

from app.providers.amap_probe import AmapProbeCapture

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPOSITORY_ROOT / "evals" / "fixtures" / "amap" / "mcp-beijing-2026-08-20.v1.json"


def load_fixture() -> tuple[AmapProbeCapture, str]:
    raw = FIXTURE_PATH.read_text(encoding="utf-8")
    return AmapProbeCapture.model_validate_json(raw), raw


def calls_by_operation(capture: AmapProbeCapture) -> dict[str, list[dict[str, Any]]]:
    calls: dict[str, list[dict[str, Any]]] = {}
    for call in capture.calls:
        calls.setdefault(call.operation, []).append(call.response)
    return calls


def test_live_fixture_matches_the_versioned_probe_contract() -> None:
    capture, _ = load_fixture()
    calls = calls_by_operation(capture)

    assert capture.data_mode == "live_capture"
    assert capture.endpoint == "https://mcp.amap.com/mcp"
    assert capture.server_name == "amap-sse-server"
    assert capture.protocol_version == "2025-03-26"
    assert len(capture.tool_catalog) == 15
    assert len(capture.calls) == 9
    assert len(calls["maps_text_search"]) == 2
    assert len(calls["maps_search_detail"]) == 2


def test_live_fixture_proves_weather_freshness_and_hotel_data_boundaries() -> None:
    capture, _ = load_fixture()
    calls = calls_by_operation(capture)

    mcp_weather = calls["maps_weather"][0]
    rest_weather = calls["weather_rest_fallback"][0]
    rest_forecast = rest_weather["forecasts"][0]
    details = calls["maps_search_detail"]

    assert set(mcp_weather) == {"city", "forecasts"}
    assert rest_forecast["adcode"] == "110000"
    assert rest_forecast["reporttime"]
    assert all(detail["location"] for detail in details)
    assert all(not detail["cost"] for detail in details)


def test_live_fixture_contains_route_summaries_but_no_credentials_or_pii() -> None:
    capture, raw = load_fixture()
    calls = calls_by_operation(capture)
    distance = calls["maps_distance"][0]["results"][0]
    walking = calls["maps_direction_walking"][0]["route"]["paths"][0]
    transit = calls["maps_direction_transit_integrated"][0]["transits"][0]

    assert int(distance["distance"]) > 0
    assert int(distance["duration"]) > 0
    assert int(walking["distance"]) > 0
    assert walking["step_count"] > 0
    assert int(transit["duration"]) > 0
    assert transit["segment_count"] > 0
    assert all("key" not in call.arguments for call in capture.calls)
    assert "?" not in capture.endpoint
    assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", raw)
    assert not re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", raw, re.I)
