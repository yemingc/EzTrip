import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.domain.provider import ProviderErrorCategory
from app.observability.redaction import TraceRedactor
from app.providers.amap_probe import (
    PROBE_CITY_ADCODE,
    REQUIRED_MCP_TOOLS,
    AmapProbeCapture,
    AmapProbeError,
    classify_amap_infocode,
    collect_amap_probe,
    decode_mcp_json,
    fetch_rest_weather,
    sanitize_detail,
    sanitize_mcp_weather,
    sanitize_text_search,
    write_probe_capture,
)


def mcp_result(
    payload: dict[str, Any],
    *,
    is_error: bool = False,
    structured_content: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=structured_content,
        isError=is_error,
    )


class FakeAmapSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> SimpleNamespace:
        names = sorted(REQUIRED_MCP_TOOLS) + [f"optional_tool_{index}" for index in range(9)]
        tools = [
            SimpleNamespace(
                name=name,
                inputSchema={"type": "object", "properties": {}, "required": []},
            )
            for name in names
        ]
        return SimpleNamespace(tools=tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((name, arguments))
        if name == "maps_text_search":
            poi_id = "B000A8UIN8" if arguments["keywords"] == "故宫博物院" else "B000A81CB2"
            return mcp_result(
                {
                    "pois": [
                        {
                            "id": poi_id,
                            "name": arguments["keywords"],
                            "address": "fixture address",
                            "typecode": "110000",
                            "tel": "13800138000",
                        }
                    ]
                }
            )
        if name == "maps_search_detail":
            is_palace = arguments["id"] == "B000A8UIN8"
            return mcp_result(
                {
                    "id": arguments["id"],
                    "name": "故宫博物院" if is_palace else "天坛公园",
                    "location": "116.397029,39.917839" if is_palace else "116.410829,39.881913",
                    "city": "北京市",
                    "type": "风景名胜",
                    "address": "fixture address",
                    "cost": [],
                    "photo": [{"url": "not retained"}],
                }
            )
        if name == "maps_weather":
            return mcp_result(
                {
                    "city": "北京市",
                    "forecasts": [
                        {
                            "date": "2026-08-20",
                            "dayweather": "晴",
                            "nightweather": "多云",
                            "daytemp": "31",
                            "nighttemp": "23",
                        }
                    ],
                    "reporttime": "must not be assumed present",
                }
            )
        if name == "maps_distance":
            return mcp_result({"results": [{"distance": "5928", "duration": "1831"}]})
        if name == "maps_direction_walking":
            return mcp_result(
                {
                    "route": {
                        "origin": arguments["origin"],
                        "destination": arguments["destination"],
                        "paths": [
                            {
                                "distance": "5508",
                                "duration": "4406",
                                "steps": [{"instruction": "向南步行", "distance": "100"}],
                            }
                        ],
                    }
                }
            )
        if name == "maps_direction_transit_integrated":
            return mcp_result(
                {
                    "origin": arguments["origin"],
                    "destination": arguments["destination"],
                    "distance": "5928",
                    "transits": [
                        {
                            "duration": "3817",
                            "walking_distance": "2876",
                            "segments": [{"walking": {}}, {"bus": {}}],
                        }
                    ],
                }
            )
        raise AssertionError(f"unexpected fake operation: {name}")


def rest_weather_payload() -> dict[str, Any]:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "forecasts": [
            {
                "province": "北京",
                "city": "北京市",
                "adcode": PROBE_CITY_ADCODE,
                "reporttime": "2026-08-20 16:03:20",
                "casts": [
                    {
                        "date": "2026-08-20",
                        "dayweather": "晴",
                        "nightweather": "多云",
                        "daytemp": "31",
                        "nighttemp": "23",
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "infocode,expected",
    [
        ("10001", ProviderErrorCategory.AUTHENTICATION_FAILED),
        ("10003", ProviderErrorCategory.RATE_LIMITED),
        ("10004", ProviderErrorCategory.RATE_LIMITED),
        ("10015", ProviderErrorCategory.TIMEOUT),
        ("20000", ProviderErrorCategory.UNRECOVERABLE),
    ],
)
def test_classify_amap_infocode(infocode: str, expected: ProviderErrorCategory) -> None:
    assert classify_amap_infocode(infocode) == expected


def test_search_and_detail_sanitizers_keep_only_contract_fields() -> None:
    search = sanitize_text_search(
        {
            "pois": [
                {
                    "id": "B000A8UIN8",
                    "name": "故宫博物院",
                    "address": "景山前街4号",
                    "typecode": "110200",
                    "tel": "13800138000",
                    "photo": "not retained",
                }
            ]
        }
    )
    detail = sanitize_detail(
        {
            "id": "B000A8UIN8",
            "name": "故宫博物院",
            "location": "116.397029,39.917839",
            "city": "北京市",
            "type": "风景名胜",
            "photo": "not retained",
        }
    )

    assert search["pois"] == [
        {
            "id": "B000A8UIN8",
            "name": "故宫博物院",
            "address": "景山前街4号",
            "typecode": "110200",
        }
    ]
    assert "photo" not in detail


def test_detail_requires_a_routeable_location() -> None:
    with pytest.raises(AmapProbeError) as error:
        sanitize_detail(
            {
                "id": "B000A8UIN8",
                "name": "故宫博物院",
                "city": "北京市",
                "type": "风景名胜",
            }
        )

    assert error.value.failure.category == ProviderErrorCategory.MISSING_FIELD


def test_mcp_weather_does_not_invent_freshness_fields() -> None:
    sanitized = sanitize_mcp_weather(
        {
            "city": "北京市",
            "forecasts": [{"date": "2026-08-20", "dayweather": "晴"}],
            "reporttime": "upstream-extra",
            "adcode": PROBE_CITY_ADCODE,
        }
    )

    assert set(sanitized) == {"city", "forecasts"}


def test_decode_mcp_json_classifies_provider_and_protocol_failures() -> None:
    with pytest.raises(AmapProbeError) as auth_error:
        decode_mcp_json(
            mcp_result({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"}),
            operation="maps_weather",
        )
    assert auth_error.value.failure.category == ProviderErrorCategory.AUTHENTICATION_FAILED
    assert auth_error.value.failure.retryable is False

    malformed = SimpleNamespace(content=[SimpleNamespace(text="not-json")], isError=False)
    with pytest.raises(AmapProbeError) as protocol_error:
        decode_mcp_json(malformed, operation="maps_weather")
    assert protocol_error.value.failure.category == ProviderErrorCategory.UNRECOVERABLE


def test_decode_mcp_json_supports_structured_multiple_and_fenced_content() -> None:
    structured = SimpleNamespace(
        content=[SimpleNamespace(text="human-readable summary")],
        structuredContent={"id": "B025301URW", "name": "泉州博物馆"},
        isError=False,
    )
    assert decode_mcp_json(structured, operation="maps_search_detail")["name"] == "泉州博物馆"

    multiple = SimpleNamespace(
        content=[
            SimpleNamespace(text="provider notice"),
            SimpleNamespace(text='{"id":"B025301URW","name":"泉州博物馆"}'),
        ],
        structuredContent=None,
        isError=False,
    )
    assert decode_mcp_json(multiple, operation="maps_search_detail")["id"] == "B025301URW"

    fenced = SimpleNamespace(
        content=[SimpleNamespace(text='```json\n{"city":"泉州市"}\n```')],
        structuredContent=None,
        isError=False,
    )
    assert decode_mcp_json(fenced, operation="maps_weather") == {"city": "泉州市"}


def test_decode_mcp_json_prioritizes_typed_tool_error_over_plain_text() -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(text="POI detail is temporarily unavailable")],
        structuredContent=None,
        isError=True,
    )

    with pytest.raises(AmapProbeError) as error:
        decode_mcp_json(result, operation="maps_search_detail")

    assert error.value.failure.category == ProviderErrorCategory.UNRECOVERABLE
    assert error.value.failure.message == "maps_search_detail returned an MCP tool error"


def test_rest_weather_preflight_preserves_freshness_without_leaking_key() -> None:
    seen_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        seen_url = str(request.url)
        return httpx.Response(200, json=rest_weather_payload())

    settings = Settings(
        _env_file=None,
        amap_maps_api_key=SecretStr("amap-fixture-secret"),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response, latency_ms = asyncio.run(fetch_rest_weather(settings, client=client))
    finally:
        asyncio.run(client.aclose())

    forecast = response["forecasts"][0]
    assert forecast["adcode"] == PROBE_CITY_ADCODE
    assert forecast["reporttime"] == "2026-08-20 16:03:20"
    assert latency_ms >= 0
    assert "amap-fixture-secret" in seen_url
    assert "amap-fixture-secret" not in json.dumps(response)


def test_collect_probe_builds_a_replayable_nine_call_capture() -> None:
    session = FakeAmapSession()
    capture = asyncio.run(
        collect_amap_probe(
            session,
            endpoint="https://mcp.amap.com/mcp",
            server_name="amap-sse-server",
            server_version="1.0.0",
            protocol_version="2025-03-26",
            rest_weather=rest_weather_payload(),
            rest_weather_latency_ms=12,
            captured_at=datetime(2026, 8, 20, 8, 30, tzinfo=UTC),
        )
    )

    assert len(capture.tool_catalog) == 15
    assert len(capture.calls) == 9
    assert len(session.calls) == 8
    assert [call.operation for call in capture.calls].count("maps_text_search") == 2
    assert session.calls[-1][0] == "maps_direction_transit_integrated"
    assert "?" not in capture.endpoint
    assert all("key" not in call.arguments for call in capture.calls)


def test_fixture_writer_redacts_secrets_and_pii(tmp_path: Path) -> None:
    session = FakeAmapSession()
    capture = asyncio.run(
        collect_amap_probe(
            session,
            endpoint="https://mcp.amap.com/mcp",
            server_name="amap-sse-server",
            server_version="1.0.0",
            protocol_version="2025-03-26",
            rest_weather=rest_weather_payload(),
            rest_weather_latency_ms=12,
            captured_at=datetime(2026, 8, 20, 8, 30, tzinfo=UTC),
        )
    )
    first_call = capture.calls[0].model_copy(
        update={"response": {"note": "amap-fixture-secret 13800138000 person@example.com"}}
    )
    capture = capture.model_copy(update={"calls": (first_call, *capture.calls[1:])})
    output_path = tmp_path / "fixture.json"

    write_probe_capture(
        capture,
        output_path,
        redactor=TraceRedactor(secrets=("amap-fixture-secret",)),
    )

    content = output_path.read_text(encoding="utf-8")
    assert "amap-fixture-secret" not in content
    assert "13800138000" not in content
    assert "person@example.com" not in content
    assert "<redacted-secret>" in content
    assert "<redacted-phone>" in content
    assert "<redacted-email>" in content


def test_capture_rejects_a_credential_bearing_endpoint() -> None:
    session = FakeAmapSession()
    capture = asyncio.run(
        collect_amap_probe(
            session,
            endpoint="https://mcp.amap.com/mcp",
            server_name="amap-sse-server",
            server_version="1.0.0",
            protocol_version="2025-03-26",
            rest_weather=rest_weather_payload(),
            rest_weather_latency_ms=12,
            captured_at=datetime(2026, 8, 20, 8, 30, tzinfo=UTC),
        )
    )

    with pytest.raises(ValueError, match="query parameters"):
        AmapProbeCapture.model_validate(
            {**capture.model_dump(mode="json"), "endpoint": "https://mcp.amap.com/mcp?key=x"}
        )
