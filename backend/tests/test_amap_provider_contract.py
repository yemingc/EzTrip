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
from app.providers.ports import POISearchRequest, RetryPolicy, RouteRequest, WeatherRiskRequest

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


def make_provider(mode: DataMode) -> AmapTravelDataProvider:
    if mode == DataMode.FIXTURE:
        return load_fixture_amap_provider()
    return AmapTravelDataProvider(
        ReplayAsLiveClient(),
        data_mode=DataMode.LIVE,
        clock=lambda: FIXED_LIVE_TIME,
    )


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
