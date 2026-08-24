import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.destinations import DestinationResolutionService, DestinationSelectionError
from app.domain.destination import DestinationResolutionStatus
from app.domain.provider import ProviderErrorCategory
from app.domain.sources import DataMode
from app.main import create_app
from app.providers.city_resolver import AmapCityResolverProvider
from app.providers.errors import ProviderRequestError


def test_fixture_resolver_separates_product_coverage_from_ambiguity() -> None:
    service = DestinationResolutionService(Settings(_env_file=None, environment="test"))

    async def exercise() -> None:
        beijing = await service.resolve("北京", data_mode=DataMode.FIXTURE)
        assert beijing.status == DestinationResolutionStatus.RESOLVED
        assert beijing.candidates[0].planning_city_name == "北京市"
        assert beijing.candidates[0].administrative_code == "110000"

        ambiguous = await service.resolve("朝阳", data_mode=DataMode.FIXTURE)
        assert ambiguous.status == DestinationResolutionStatus.AMBIGUOUS
        assert [candidate.qualified_name for candidate in ambiguous.candidates] == [
            "北京市朝阳区",
            "辽宁省朝阳市",
        ]
        assert ambiguous.select("211300").planning_city_name == "朝阳市"
        with pytest.raises(DestinationSelectionError) as ambiguity_error:
            await service.resolve_and_select(
                "朝阳",
                data_mode=DataMode.FIXTURE,
                selected_adcode=None,
            )
        assert ambiguity_error.value.error_code == "destination-ambiguous"

        with pytest.raises(DestinationSelectionError) as stale_selection:
            await service.resolve_and_select(
                "北京",
                data_mode=DataMode.FIXTURE,
                selected_adcode="310000",
            )
        assert stale_selection.value.error_code == "destination-selection-invalid"

        unsupported = await service.resolve("泉州", data_mode=DataMode.FIXTURE)
        assert unsupported.status == DestinationResolutionStatus.UNSUPPORTED
        with pytest.raises(DestinationSelectionError) as error:
            await service.resolve_and_select(
                "泉州",
                data_mode=DataMode.FIXTURE,
                selected_adcode=None,
            )
        assert error.value.error_code == "destination-fixture-unsupported"

    asyncio.run(exercise())


def test_amap_resolver_normalizes_city_and_municipality_results() -> None:
    seen_address = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_address
        seen_address = request.url.params["address"]
        return httpx.Response(
            200,
            json={
                "status": "1",
                "info": "OK",
                "infocode": "10000",
                "count": "2",
                "geocodes": [
                    {
                        "formatted_address": "福建省泉州市",
                        "province": "福建省",
                        "city": "泉州市",
                        "district": [],
                        "adcode": "350500",
                        "location": "118.675675,24.874132",
                        "level": "市",
                    },
                    {
                        "formatted_address": "北京市朝阳区",
                        "province": "北京市",
                        "city": [],
                        "district": "朝阳区",
                        "adcode": "110105",
                        "location": "116.443108,39.921470",
                        "level": "区县",
                    },
                ],
            },
        )

    settings = Settings(
        _env_file=None,
        environment="test",
        amap_maps_api_key=SecretStr("amap-test-key"),
    )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = AmapCityResolverProvider(
                settings,
                client,
                clock=lambda: datetime(2026, 8, 24, tzinfo=UTC),
            )
            return await provider.resolve_destination("朝阳")

    result = asyncio.run(exercise())
    assert seen_address == "朝阳"
    assert result.status == DestinationResolutionStatus.AMBIGUOUS
    assert [candidate.planning_city_name for candidate in result.candidates] == [
        "泉州市",
        "北京市",
    ]
    assert all(candidate.source.provider == "amap-geocode-rest" for candidate in result.candidates)


def test_live_resolution_is_enabled_by_request_when_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_address = ""
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_address
        seen_address = request.url.params["address"]
        return httpx.Response(
            200,
            json={
                "status": "1",
                "info": "OK",
                "infocode": "10000",
                "count": "1",
                "geocodes": [
                    {
                        "formatted_address": "福建省泉州市",
                        "province": "福建省",
                        "city": "泉州市",
                        "district": [],
                        "adcode": "350500",
                        "location": "118.675675,24.874132",
                        "level": "市",
                    }
                ],
            },
        )

    class MockClientContext:
        def __init__(self) -> None:
            self.client = real_async_client(transport=httpx.MockTransport(handler))

        async def __aenter__(self) -> httpx.AsyncClient:
            return self.client

        async def __aexit__(self, *args: object) -> None:
            await self.client.aclose()

    monkeypatch.setattr(
        "app.destinations.service.httpx.AsyncClient",
        lambda **kwargs: MockClientContext(),
    )
    service = DestinationResolutionService(
        Settings(
            _env_file=None,
            environment="test",
            amap_maps_api_key=SecretStr("amap-test-key"),
        )
    )

    result = asyncio.run(service.resolve("泉州", data_mode=DataMode.LIVE))

    assert seen_address == "泉州"
    assert result.status == DestinationResolutionStatus.RESOLVED
    assert result.candidates[0].administrative_code == "350500"


def test_amap_resolver_returns_typed_no_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "status": "1",
                "info": "OK",
                "infocode": "10000",
                "count": "0",
                "geocodes": [],
            },
        )

    settings = Settings(
        _env_file=None,
        environment="test",
        amap_maps_api_key=SecretStr("amap-test-key"),
    )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await AmapCityResolverProvider(settings, client).resolve_destination(
                "不存在的城市"
            )

    result = asyncio.run(exercise())
    assert result.status == DestinationResolutionStatus.NO_RESULT
    assert result.candidates == ()


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (429, {"status": "0"}, ProviderErrorCategory.RATE_LIMITED),
        (
            200,
            {"status": "0", "infocode": "10001", "info": "INVALID_USER_KEY"},
            ProviderErrorCategory.AUTHENTICATION_FAILED,
        ),
    ],
)
def test_amap_resolver_maps_provider_failures(
    status_code: int,
    payload: dict[str, object],
    expected: ProviderErrorCategory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status_code, json=payload)

    settings = Settings(
        _env_file=None,
        environment="test",
        amap_maps_api_key=SecretStr("amap-test-key"),
    )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await AmapCityResolverProvider(settings, client).resolve_destination("泉州")

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(exercise())
    assert error.value.failure.category == expected


def test_destination_resolution_api_exposes_fixture_boundaries() -> None:
    app = create_app(settings=Settings(_env_file=None, environment="test"))
    with TestClient(app) as client:
        resolved = client.post(
            "/api/destinations/resolve",
            json={"input_name": "上海", "data_mode": "fixture"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["candidates"][0]["administrative_code"] == "310000"

        ambiguous = client.post(
            "/api/destinations/resolve",
            json={"input_name": "朝阳", "data_mode": "fixture"},
        )
        assert ambiguous.status_code == 200
        assert ambiguous.json()["status"] == "ambiguous"
        assert len(ambiguous.json()["candidates"]) == 2

        unsupported = client.post(
            "/api/destinations/resolve",
            json={"input_name": "泉州", "data_mode": "fixture"},
        )
        assert unsupported.status_code == 200
        assert unsupported.json()["status"] == "unsupported"

        live_without_key = client.post(
            "/api/destinations/resolve",
            json={"input_name": "泉州", "data_mode": "live"},
        )
        assert live_without_key.status_code == 409
        assert live_without_key.json()["detail"] == {
            "error_code": "destination-resolution-configuration",
            "message": "服务端尚未配置高德 Key, 无法使用实时城市解析。",
        }
