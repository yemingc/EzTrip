import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app.domain.candidates import ActivityEnvironment
from app.domain.provider import ProviderErrorCategory
from app.domain.sources import DataMode
from app.domain.travel_data import RiskSeverity, RouteEndpoint, RouteMode, WeatherRiskType
from app.providers.amap_adapter import AmapTravelDataProvider, load_fixture_amap_provider
from app.providers.amap_clients import AmapFixtureToolClient
from app.providers.amap_protocol import build_amap_failure
from app.providers.errors import ProviderRequestError
from app.providers.ports import (
    POISearchRequest,
    RetryPolicy,
    RouteRequest,
    StaySearchRequest,
    WeatherRiskRequest,
)

FIXED_LIVE_TIME = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)


class ReplayAsLiveClient:
    captured_at: datetime | None = None

    def __init__(self) -> None:
        self._fixture = AmapFixtureToolClient()

    async def call_tool(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        return await self._fixture.call_tool(operation, arguments)

    async def fetch_weather_freshness(self, city_adcode: str) -> dict[str, object]:
        return await self._fixture.fetch_weather_freshness(city_adcode)


class SyntheticHotelClient:
    captured_at: datetime | None = FIXED_LIVE_TIME

    def __init__(self, *, include_hotels: bool = True) -> None:
        hotel_type = "住宿服务;宾馆酒店" if include_hotels else "餐饮服务;中餐厅"
        first_name = "前门示例酒店" if include_hotels else "前门示例餐厅"
        second_name = "西单示例旅馆" if include_hotels else "西单示例茶馆"
        self.details: dict[str, dict[str, object]] = {
            "HOTEL001": {
                "id": "HOTEL001",
                "name": first_name,
                "city": "北京",
                "district": "东城区",
                "address": "前门示例路 1 号",
                "location": "116.397000,39.899000",
                "type": hotel_type,
                "rating": "4.7",
            },
            "FOOD001": {
                "id": "FOOD001",
                "name": "示例餐厅",
                "city": "北京",
                "district": "东城区",
                "address": "东城示例路 2 号",
                "location": "116.399000,39.901000",
                "type": "餐饮服务;中餐厅",
            },
            "HOTEL002": {
                "id": "HOTEL002",
                "name": second_name,
                "city": "北京",
                "district": "西城区",
                "address": "西单示例路 3 号",
                "location": "116.374000,39.908000",
                "type": hotel_type,
                "level": "经济型",
            },
        }

    async def call_tool(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        del arguments
        if operation == "maps_text_search":
            return {"pois": [{"id": provider_id} for provider_id in self.details]}
        if operation == "maps_search_detail":
            raise AssertionError("detail calls require the provider id argument")
        raise AssertionError(f"unexpected operation: {operation}")

    async def fetch_weather_freshness(self, city_adcode: str) -> dict[str, object]:
        raise AssertionError(f"unexpected weather freshness request: {city_adcode}")


class SyntheticHotelDetailClient(SyntheticHotelClient):
    async def call_tool(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        if operation == "maps_text_search":
            return {"pois": [{"id": provider_id} for provider_id in self.details]}
        if operation == "maps_search_detail":
            return self.details[str(arguments["id"])]
        raise AssertionError(f"unexpected operation: {operation}")


def make_provider(mode: DataMode) -> AmapTravelDataProvider:
    if mode == DataMode.FIXTURE:
        return load_fixture_amap_provider()
    return AmapTravelDataProvider(
        ReplayAsLiveClient(),
        data_mode=DataMode.LIVE,
        clock=lambda: FIXED_LIVE_TIME,
    )


def test_stay_search_filters_hotel_pois_without_inventing_commercial_facts() -> None:
    provider = AmapTravelDataProvider(
        SyntheticHotelDetailClient(),
        data_mode=DataMode.FIXTURE,
    )

    stays = asyncio.run(
        provider.search_stays(
            StaySearchRequest(keywords="北京中心住宿", city_adcode="110000", limit=3)
        )
    )

    assert [item.candidate_id for item in stays] == [
        "amap-stay-hotel001",
        "amap-stay-hotel002",
    ]
    assert [item.area_name for item in stays] == ["东城区", "西城区"]
    assert "category:住宿服务" in stays[0].tags
    assert all(not tag.startswith(("rating:", "level:")) for tag in stays[0].tags)
    assert stays[0].nightly_price_estimate is None
    assert stays[0].price_basis is None
    assert stays[0].price_source is None
    assert stays[0].availability_status == "unknown"
    assert stays[0].booking_supported is False
    assert stays[0].source.provider_id == "HOTEL001"
    assert stays[0].source.data_mode == DataMode.FIXTURE


def test_stay_search_returns_typed_empty_result_when_no_poi_is_hotel_classified() -> None:
    provider = AmapTravelDataProvider(
        SyntheticHotelDetailClient(include_hotels=False),
        data_mode=DataMode.FIXTURE,
    )

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(
            provider.search_stays(
                StaySearchRequest(keywords="北京中心住宿", city_adcode="110000", limit=3)
            )
        )

    assert error.value.failure.category == ProviderErrorCategory.EMPTY_RESULT
    assert error.value.failure.retryable is False


async def collect_contract_outputs(
    provider: AmapTravelDataProvider,
) -> tuple[object, object, tuple[object, ...], object, object]:
    palace = (
        await provider.search_pois(POISearchRequest(keywords="故宫博物院", city_adcode="110000"))
    )[0]
    temple = (
        await provider.search_pois(POISearchRequest(keywords="天坛公园", city_adcode="110000"))
    )[0]
    risks = await provider.get_weather_risks(WeatherRiskRequest(city_adcode="110000"))
    walking = await provider.get_route(
        RouteRequest(
            origin=RouteEndpoint(
                name=palace.name,
                candidate_id=palace.candidate_id,
                location=palace.location,
            ),
            destination=RouteEndpoint(
                name=temple.name,
                candidate_id=temple.candidate_id,
                location=temple.location,
            ),
            mode=RouteMode.WALKING,
            city_adcode="110000",
        )
    )
    transit = await provider.get_route(
        RouteRequest(
            origin=RouteEndpoint(
                name=palace.name,
                candidate_id=palace.candidate_id,
                location=palace.location,
            ),
            destination=RouteEndpoint(
                name=temple.name,
                candidate_id=temple.candidate_id,
                location=temple.location,
            ),
            mode=RouteMode.TRANSIT,
            city_adcode="110000",
        )
    )
    return palace, temple, risks, walking, transit


@pytest.mark.parametrize("mode", [DataMode.FIXTURE, DataMode.LIVE])
def test_live_and_fixture_modes_satisfy_the_same_domain_contract(mode: DataMode) -> None:
    palace, temple, risks, walking, transit = asyncio.run(
        collect_contract_outputs(make_provider(mode))
    )

    assert palace.candidate_id == "amap-poi-b000a8uin8"
    assert palace.name == "故宫博物院"
    assert palace.location.model_dump() == {
        "latitude": 39.917839,
        "longitude": 116.397029,
    }
    assert palace.environment == ActivityEnvironment.MIXED
    assert palace.source.provider == "amap"
    assert palace.source.provider_id == "B000A8UIN8"
    assert palace.source.data_mode == mode
    assert len(palace.source.raw_response_sha256 or "") == 64

    assert temple.name == "天坛公园"
    assert temple.environment == ActivityEnvironment.OUTDOOR
    assert len(risks) == 3
    assert {risk.risk_type for risk in risks} == {WeatherRiskType.RAIN}
    assert {risk.severity for risk in risks} == {RiskSeverity.MEDIUM}
    assert all(risk.source.data_mode == mode for risk in risks)
    assert all(risk.source.retrieved_at.utcoffset().total_seconds() == 28800 for risk in risks)

    assert walking.mode == RouteMode.WALKING
    assert walking.distance_meters == 5508
    assert walking.duration_minutes == 74
    assert transit.mode == RouteMode.TRANSIT
    assert transit.distance_meters == 5172
    assert transit.duration_minutes == 64
    assert walking.source.data_mode == mode
    assert transit.source.data_mode == mode


class FailingThenReplayClient(ReplayAsLiveClient):
    def __init__(self, failures: list[ProviderErrorCategory]) -> None:
        super().__init__()
        self.failures = failures
        self.search_attempts = 0

    async def call_tool(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        if operation == "maps_text_search":
            self.search_attempts += 1
            if self.failures:
                category = self.failures.pop(0)
                raise ProviderRequestError(
                    build_amap_failure(
                        operation=operation,
                        category=category,
                        message=f"injected {category.value}",
                    )
                )
        return await super().call_tool(operation, arguments)


def test_retryable_failures_use_bounded_exponential_backoff() -> None:
    client = FailingThenReplayClient(
        [ProviderErrorCategory.TIMEOUT, ProviderErrorCategory.RATE_LIMITED]
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    provider = AmapTravelDataProvider(
        client,
        data_mode=DataMode.LIVE,
        retry_policy=RetryPolicy(
            max_attempts=3,
            base_delay_seconds=0.1,
            max_delay_seconds=0.5,
        ),
        sleep=record_sleep,
        clock=lambda: FIXED_LIVE_TIME,
    )

    candidates = asyncio.run(
        provider.search_pois(POISearchRequest(keywords="故宫博物院", city_adcode="110000"))
    )

    assert candidates[0].name == "故宫博物院"
    assert client.search_attempts == 3
    assert delays == [0.1, 0.2]


def test_authentication_failure_is_not_retried() -> None:
    client = FailingThenReplayClient([ProviderErrorCategory.AUTHENTICATION_FAILED])
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    provider = AmapTravelDataProvider(
        client,
        data_mode=DataMode.LIVE,
        retry_policy=RetryPolicy(max_attempts=3),
        sleep=record_sleep,
        clock=lambda: FIXED_LIVE_TIME,
    )

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(
            provider.search_pois(POISearchRequest(keywords="故宫博物院", city_adcode="110000"))
        )

    assert error.value.failure.category == ProviderErrorCategory.AUTHENTICATION_FAILED
    assert client.search_attempts == 1
    assert delays == []


def test_fixture_rejects_unrecorded_requests_as_non_retryable_empty_results() -> None:
    provider = load_fixture_amap_provider(retry_policy=RetryPolicy(max_attempts=3))

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(
            provider.search_pois(POISearchRequest(keywords="未录制景点", city_adcode="110000"))
        )

    assert error.value.failure.category == ProviderErrorCategory.EMPTY_RESULT
    assert error.value.failure.retryable is False


def test_unsupported_route_mode_fails_before_any_provider_call() -> None:
    provider = make_provider(DataMode.FIXTURE)
    endpoint = RouteEndpoint(
        name="故宫博物院",
        location={"latitude": 39.917839, "longitude": 116.397029},
    )
    destination = RouteEndpoint(
        name="天坛公园",
        location={"latitude": 39.881913, "longitude": 116.410829},
    )

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(
            provider.get_route(
                RouteRequest(
                    origin=endpoint,
                    destination=destination,
                    mode=RouteMode.DRIVING,
                    city_adcode="110000",
                )
            )
        )

    assert error.value.failure.category == ProviderErrorCategory.UNRECOVERABLE
    assert error.value.failure.retryable is False
