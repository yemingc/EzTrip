import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

import app.providers.amap_clients as amap_clients
from app.core.config import Settings
from app.domain.provider import ProviderErrorCategory
from app.domain.sources import DataMode
from app.domain.travel_data import RiskSeverity, WeatherRiskType
from app.providers.amap_adapter import AmapTravelDataProvider
from app.providers.amap_clients import AmapFixtureToolClient, AmapLiveToolClient
from app.providers.errors import ProviderRequestError
from app.providers.ports import POISearchRequest, WeatherRiskRequest


class SyntheticWeatherClient:
    captured_at: datetime | None = None

    async def call_tool(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        assert operation == "maps_weather"
        assert arguments == {"city": "110000"}
        return {
            "city": "北京市",
            "forecasts": [
                {
                    "date": "2026-12-01",
                    "dayweather": "暴雪",
                    "nightweather": "大暴雨",
                    "daytemp_float": "40.0",
                    "nighttemp_float": "-12.0",
                    "daypower": "9-10",
                    "nightpower": "7",
                }
            ],
        }

    async def fetch_weather_freshness(self, city_adcode: str) -> dict[str, object]:
        assert city_adcode == "110000"
        return {
            "forecasts": [
                {
                    "city": "北京市",
                    "adcode": "110000",
                    "reporttime": "2026-11-30 20:00:00",
                    "casts": [{"date": "2026-12-01"}],
                }
            ]
        }


def test_weather_thresholds_are_deterministic_and_can_emit_multiple_risks() -> None:
    provider = AmapTravelDataProvider(
        SyntheticWeatherClient(),
        data_mode=DataMode.LIVE,
        clock=lambda: datetime(2026, 11, 30, 12, 0, tzinfo=UTC),
    )

    risks = asyncio.run(provider.get_weather_risks(WeatherRiskRequest(city_adcode="110000")))
    by_type = {risk.risk_type: risk for risk in risks}

    assert set(by_type) == {
        WeatherRiskType.RAIN,
        WeatherRiskType.SNOW,
        WeatherRiskType.HEAT,
        WeatherRiskType.COLD,
        WeatherRiskType.WIND,
    }
    assert by_type[WeatherRiskType.RAIN].severity == RiskSeverity.EXTREME
    assert by_type[WeatherRiskType.SNOW].severity == RiskSeverity.HIGH
    assert by_type[WeatherRiskType.HEAT].severity == RiskSeverity.EXTREME
    assert by_type[WeatherRiskType.COLD].severity == RiskSeverity.HIGH
    assert by_type[WeatherRiskType.WIND].severity == RiskSeverity.HIGH


class MissingLocationClient:
    captured_at = datetime(2026, 8, 20, tzinfo=UTC)

    async def call_tool(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        if operation == "maps_text_search":
            return {"pois": [{"id": "B000A8UIN8", "name": "故宫博物院"}]}
        return {
            "id": "B000A8UIN8",
            "name": "故宫博物院",
            "city": "北京市",
            "type": "风景名胜",
        }

    async def fetch_weather_freshness(self, city_adcode: str) -> dict[str, object]:
        raise AssertionError("weather should not be called")


def test_poi_field_drift_becomes_a_typed_missing_field_failure() -> None:
    provider = AmapTravelDataProvider(
        MissingLocationClient(),
        data_mode=DataMode.LIVE,
    )

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(
            provider.search_pois(POISearchRequest(keywords="故宫博物院", city_adcode="110000"))
        )

    assert error.value.failure.operation == "maps_search_detail"
    assert error.value.failure.category == ProviderErrorCategory.MISSING_FIELD


def test_fixture_loader_rejects_malformed_or_missing_files(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{}", encoding="utf-8")

    for fixture_path in (malformed, tmp_path / "missing.json"):
        with pytest.raises(ProviderRequestError) as error:
            AmapFixtureToolClient(fixture_path)
        assert error.value.failure.operation == "load_fixture"
        assert error.value.failure.category == ProviderErrorCategory.UNRECOVERABLE


def test_live_client_requires_a_key_and_an_open_session() -> None:
    settings = Settings(_env_file=None)
    client = AmapLiveToolClient(settings)

    with pytest.raises(ProviderRequestError) as key_error:
        asyncio.run(client.__aenter__())
    assert key_error.value.failure.category == ProviderErrorCategory.AUTHENTICATION_FAILED

    with pytest.raises(ProviderRequestError) as session_error:
        asyncio.run(client.call_tool("maps_weather", {"city": "110000"}))
    assert session_error.value.failure.category == ProviderErrorCategory.UNRECOVERABLE


def weather_freshness_payload() -> dict[str, object]:
    return {
        "forecasts": [
            {
                "city": "北京市",
                "adcode": "110000",
                "reporttime": "2026-08-20 16:37:40",
                "casts": [],
            }
        ]
    }


class FakeMcpSession:
    def __init__(self, read_stream: object, write_stream: object) -> None:
        self.closed = False

    async def __aenter__(self) -> "FakeMcpSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def initialize(self) -> SimpleNamespace:
        return SimpleNamespace()

    async def call_tool(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> SimpleNamespace:
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    text=json.dumps(
                        {
                            "city": "北京市",
                            "forecasts": [{"date": "2026-08-20", "dayweather": "晴"}],
                        },
                        ensure_ascii=False,
                    )
                )
            ],
            isError=False,
        )


def install_fake_live_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_type: type[FakeMcpSession] = FakeMcpSession,
) -> list[str]:
    urls: list[str] = []

    async def fake_fetch_weather_from_rest(
        self: AmapLiveToolClient,
        city_adcode: str,
    ) -> dict[str, object]:
        assert city_adcode == "110000"
        return weather_freshness_payload()

    @asynccontextmanager
    async def fake_streamable_http_client(
        url: str,
        *,
        http_client: httpx.AsyncClient,
    ) -> Any:
        urls.append(url)
        yield object(), object(), lambda: "fake-session"

    monkeypatch.setattr(
        AmapLiveToolClient,
        "_fetch_weather_from_rest",
        fake_fetch_weather_from_rest,
    )
    monkeypatch.setattr(amap_clients, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(amap_clients, "ClientSession", session_type)
    return urls


def test_live_client_opens_one_session_decodes_tools_and_reuses_weather_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = install_fake_live_transport(monkeypatch)
    settings = Settings(
        _env_file=None,
        amap_maps_api_key=SecretStr("amap-fake-live-key"),
    )
    client = AmapLiveToolClient(settings)

    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        async with client:
            payload = await client.call_tool("maps_weather", {"city": "110000"})
            freshness = await client.fetch_weather_freshness("110000")
            return payload, freshness

    payload, freshness = asyncio.run(exercise())

    assert payload["city"] == "北京市"
    assert freshness == weather_freshness_payload()
    assert len(urls) == 1
    assert "amap-fake-live-key" in urls[0]

    with pytest.raises(ProviderRequestError) as closed_error:
        asyncio.run(client.fetch_weather_freshness("110000"))
    assert closed_error.value.failure.category == ProviderErrorCategory.UNRECOVERABLE


class TimingOutMcpSession(FakeMcpSession):
    async def call_tool(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> SimpleNamespace:
        raise httpx.ReadTimeout("injected timeout")


def test_live_client_maps_transport_timeout_to_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_live_transport(monkeypatch, session_type=TimingOutMcpSession)
    settings = Settings(
        _env_file=None,
        amap_maps_api_key=SecretStr("amap-fake-live-key"),
    )

    async def exercise() -> None:
        async with AmapLiveToolClient(settings) as client:
            await client.call_tool("maps_weather", {"city": "110000"})

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(exercise())

    assert error.value.failure.category == ProviderErrorCategory.TIMEOUT
    assert error.value.failure.retryable is True


def test_live_weather_freshness_uses_the_requested_city_not_the_probe_city() -> None:
    seen_city = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_city
        seen_city = request.url.params["city"]
        return httpx.Response(
            200,
            json={
                "status": "1",
                "info": "OK",
                "infocode": "10000",
                "forecasts": [
                    {
                        "city": "上海市",
                        "adcode": "310000",
                        "reporttime": "2026-08-20 19:00:00",
                        "casts": [],
                    }
                ],
            },
        )

    settings = Settings(
        _env_file=None,
        amap_maps_api_key=SecretStr("amap-fake-live-key"),
    )
    provider_client = AmapLiveToolClient(settings)

    async def exercise() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            provider_client._http_client = http_client
            return await provider_client.fetch_weather_freshness("310000")

    response = asyncio.run(exercise())

    assert seen_city == "310000"
    assert response["forecasts"][0]["adcode"] == "310000"


@pytest.mark.parametrize(
    "status_code,payload,expected",
    [
        (429, {}, ProviderErrorCategory.RATE_LIMITED),
        (
            200,
            {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"},
            ProviderErrorCategory.AUTHENTICATION_FAILED,
        ),
    ],
)
def test_live_weather_freshness_classifies_http_and_amap_failures(
    status_code: int,
    payload: dict[str, object],
    expected: ProviderErrorCategory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    settings = Settings(
        _env_file=None,
        amap_maps_api_key=SecretStr("amap-fake-live-key"),
    )
    provider_client = AmapLiveToolClient(settings)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            provider_client._http_client = http_client
            await provider_client.fetch_weather_freshness("310000")

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(exercise())

    assert error.value.failure.category == expected
