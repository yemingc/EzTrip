import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import TypeVar

from app.core.config import Settings
from app.domain.candidates import ActivityEnvironment, CandidatePOI, CandidateStay, GeoPoint
from app.domain.provider import ProviderErrorCategory
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import (
    RiskSeverity,
    RouteLeg,
    RouteMode,
    WeatherRisk,
    WeatherRiskType,
)
from app.providers.amap_clients import (
    DEFAULT_AMAP_FIXTURE_PATH,
    AmapFixtureToolClient,
    AmapLiveToolClient,
    AmapToolClient,
)
from app.providers.amap_protocol import build_amap_failure
from app.providers.errors import ProviderRequestError
from app.providers.ports import (
    POISearchRequest,
    RetryPolicy,
    RouteRequest,
    Sleep,
    StaySearchRequest,
    WeatherRiskRequest,
)

BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
T = TypeVar("T")
RiskSpec = tuple[WeatherRiskType, RiskSeverity, dict[str, float], str, str]
DEFAULT_RETRY_POLICY = RetryPolicy()

INDOOR_TERMS = ("博物馆", "美术馆", "科技馆", "展览馆", "室内", "剧院", "影院")
OUTDOOR_TERMS = ("公园", "风景名胜", "世界遗产", "山", "湖", "古迹", "广场")
HOTEL_TERMS = ("住宿服务", "宾馆酒店", "酒店", "旅馆", "公寓式酒店")


def _provider_error(
    operation: str,
    category: ProviderErrorCategory,
    message: str,
) -> ProviderRequestError:
    return ProviderRequestError(
        build_amap_failure(operation=operation, category=category, message=message)
    )


def _as_object(value: object, *, operation: str, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _provider_error(
            operation,
            ProviderErrorCategory.MISSING_FIELD,
            f"{operation} response field {field} is not an object",
        )
    return {str(key): item for key, item in value.items()}


def _as_records(value: object, *, operation: str, field: str) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _provider_error(
            operation,
            ProviderErrorCategory.MISSING_FIELD,
            f"{operation} response field {field} is not a list",
        )
    records = [_as_object(item, operation=operation, field=field) for item in value]
    if not records:
        raise _provider_error(
            operation,
            ProviderErrorCategory.EMPTY_RESULT,
            f"{operation} returned no {field}",
        )
    return records


def _required_text(payload: Mapping[str, object], field: str, *, operation: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _provider_error(
            operation,
            ProviderErrorCategory.MISSING_FIELD,
            f"{operation} response has no usable {field}",
        )
    return value.strip()


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_location(value: object, *, operation: str) -> GeoPoint:
    if not isinstance(value, str):
        raise _provider_error(
            operation,
            ProviderErrorCategory.MISSING_FIELD,
            f"{operation} response has no usable location",
        )
    parts = value.split(",")
    if len(parts) != 2:
        raise _provider_error(
            operation,
            ProviderErrorCategory.MISSING_FIELD,
            f"{operation} response has no usable location",
        )
    try:
        longitude, latitude = (float(part) for part in parts)
        return GeoPoint(latitude=latitude, longitude=longitude)
    except ValueError as exc:
        raise _provider_error(
            operation,
            ProviderErrorCategory.MISSING_FIELD,
            f"{operation} response has an invalid location",
        ) from exc


def _payload_sha256(*payloads: Mapping[str, object]) -> str:
    serialized = json.dumps(
        payloads,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _split_categories(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ("未分类",)
    categories: list[str] = []
    for category in re.split(r"[;|]", value):
        normalized = category.strip()
        if normalized and normalized not in categories:
            categories.append(normalized)
    return tuple(categories) or ("未分类",)


def _classify_environment(categories: tuple[str, ...], name: str) -> ActivityEnvironment:
    text = " ".join((*categories, name))
    has_indoor = any(term in text for term in INDOOR_TERMS)
    has_outdoor = any(term in text for term in OUTDOOR_TERMS)
    if has_indoor and has_outdoor:
        return ActivityEnvironment.MIXED
    if has_indoor:
        return ActivityEnvironment.INDOOR
    if has_outdoor:
        return ActivityEnvironment.OUTDOOR
    return ActivityEnvironment.UNKNOWN


def _coordinate(point: GeoPoint) -> str:
    return f"{point.longitude:.6f},{point.latitude:.6f}"


def _positive_int(value: object, *, operation: str, field: str) -> int:
    if not isinstance(value, (int, float, str)):
        raise _provider_error(
            operation,
            ProviderErrorCategory.MISSING_FIELD,
            f"{operation} response has no numeric {field}",
        )
    try:
        parsed = math.ceil(float(value))
    except (TypeError, ValueError) as exc:
        raise _provider_error(
            operation,
            ProviderErrorCategory.MISSING_FIELD,
            f"{operation} response has no numeric {field}",
        ) from exc
    if parsed <= 0:
        raise _provider_error(
            operation,
            ProviderErrorCategory.MISSING_FIELD,
            f"{operation} response has non-positive {field}",
        )
    return parsed


def _parse_reporttime(value: object) -> datetime:
    if not isinstance(value, str):
        raise _provider_error(
            "weather_rest_fallback",
            ProviderErrorCategory.MISSING_FIELD,
            "weather freshness has no reporttime",
        )
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TIMEZONE)
    except ValueError as exc:
        raise _provider_error(
            "weather_rest_fallback",
            ProviderErrorCategory.MISSING_FIELD,
            "weather freshness has an invalid reporttime",
        ) from exc


def _severity_for_rain(weather_text: str) -> RiskSeverity:
    if any(term in weather_text for term in ("特大暴雨", "大暴雨")):
        return RiskSeverity.EXTREME
    if any(term in weather_text for term in ("暴雨", "大雨")):
        return RiskSeverity.HIGH
    if any(term in weather_text for term in ("雷阵雨", "中雨")):
        return RiskSeverity.MEDIUM
    return RiskSeverity.LOW


def _temperature(value: object) -> float | None:
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wind_power(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    powers = [int(item) for item in re.findall(r"\d+", value)]
    return max(powers) if powers else None


def _risk_specs(forecast: Mapping[str, object]) -> list[RiskSpec]:
    day_weather = str(forecast.get("dayweather", ""))
    night_weather = str(forecast.get("nightweather", ""))
    weather_text = f"{day_weather}/{night_weather}"
    specs: list[RiskSpec] = []

    if "雨" in weather_text:
        specs.append(
            (
                WeatherRiskType.RAIN,
                _severity_for_rain(weather_text),
                {},
                f"高德预报天气包含: {weather_text}",
                "优先安排室内活动, 并在出发前复核最新预报。",
            )
        )
    if "雪" in weather_text:
        severity = RiskSeverity.HIGH if "暴雪" in weather_text else RiskSeverity.MEDIUM
        specs.append(
            (
                WeatherRiskType.SNOW,
                severity,
                {},
                f"高德预报天气包含: {weather_text}",
                "减少长距离户外步行, 并预留交通延误时间。",
            )
        )

    day_temperature = _temperature(forecast.get("daytemp_float", forecast.get("daytemp")))
    if day_temperature is not None and day_temperature >= 35:
        severity = RiskSeverity.MEDIUM
        if day_temperature >= 40:
            severity = RiskSeverity.EXTREME
        elif day_temperature >= 38:
            severity = RiskSeverity.HIGH
        specs.append(
            (
                WeatherRiskType.HEAT,
                severity,
                {"day_temperature_c": day_temperature},
                "日间最高温达到或超过 35°C",
                "避开正午长时间户外活动, 补水并安排室内休息。",
            )
        )

    night_temperature = _temperature(forecast.get("nighttemp_float", forecast.get("nighttemp")))
    if night_temperature is not None and night_temperature <= 0:
        severity = RiskSeverity.HIGH if night_temperature <= -10 else RiskSeverity.MEDIUM
        specs.append(
            (
                WeatherRiskType.COLD,
                severity,
                {"night_temperature_c": night_temperature},
                "夜间最低温达到或低于 0°C",
                "增加保暖装备, 并减少夜间户外停留。",
            )
        )

    wind_powers = [
        item
        for item in (
            _wind_power(forecast.get("daypower")),
            _wind_power(forecast.get("nightpower")),
        )
        if item is not None
    ]
    wind_power = max(wind_powers, default=None)
    if wind_power is not None and wind_power >= 6:
        severity = RiskSeverity.HIGH if wind_power >= 9 else RiskSeverity.MEDIUM
        specs.append(
            (
                WeatherRiskType.WIND,
                severity,
                {"wind_power_level": float(wind_power)},
                "预报风力达到或超过 6 级",
                "避免高处和开阔区域活动, 并复核景区临时关闭信息。",
            )
        )
    return specs


class AmapTravelDataProvider:
    """Normalize AMap tool responses into stable EzTrip domain DTOs."""

    def __init__(
        self,
        client: AmapToolClient,
        *,
        data_mode: DataMode,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if data_mode not in {DataMode.LIVE, DataMode.FIXTURE}:
            raise ValueError("AMap provider data_mode must be live or fixture")
        self._client = client
        self._data_mode = data_mode
        self._retry_policy = retry_policy
        self._sleep = sleep
        self._clock = clock

    def _retrieved_at(self) -> datetime:
        return self._client.captured_at or self._clock()

    async def _invoke(self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                return await call()
            except ProviderRequestError as exc:
                if not exc.failure.retryable or attempt >= self._retry_policy.max_attempts:
                    raise
                delay = exc.failure.retry_after_seconds or self._retry_policy.delay_for_retry(
                    attempt
                )
                await self._sleep(delay)
        raise AssertionError(f"retry loop for {operation} exited unexpectedly")

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        operation = "maps_text_search"
        search_payload = await self._invoke(
            operation,
            lambda: self._client.call_tool(
                operation,
                {
                    "keywords": request.keywords,
                    "city": request.city_adcode,
                    "citylimit": True,
                },
            ),
        )
        search_results = _as_records(
            search_payload.get("pois"),
            operation=operation,
            field="pois",
        )
        candidates: list[CandidatePOI] = []
        detail_failures: list[ProviderRequestError] = []
        detail_operation = "maps_search_detail"
        for search_result in search_results[: request.limit]:
            try:
                provider_id = _required_text(search_result, "id", operation=operation)
                detail_payload = await self._invoke(
                    detail_operation,
                    partial(
                        self._client.call_tool,
                        detail_operation,
                        {"id": provider_id},
                    ),
                )
                detail_id = _required_text(detail_payload, "id", operation=detail_operation)
                if detail_id != provider_id:
                    raise _provider_error(
                        detail_operation,
                        ProviderErrorCategory.MISSING_FIELD,
                        "maps_search_detail returned a different POI id",
                    )
                name = _required_text(detail_payload, "name", operation=detail_operation)
                city = _required_text(detail_payload, "city", operation=detail_operation)
                categories = _split_categories(detail_payload.get("type"))
                tags = tuple(
                    tag
                    for tag in (
                        f"level:{detail_payload['level']}"
                        if _optional_text(detail_payload.get("level"))
                        else None,
                        f"rating:{detail_payload['rating']}"
                        if _optional_text(detail_payload.get("rating"))
                        else None,
                    )
                    if tag is not None
                )
                candidates.append(
                    CandidatePOI(
                        candidate_id=f"amap-poi-{provider_id.casefold()}",
                        name=name,
                        city=city,
                        district=_optional_text(detail_payload.get("district")),
                        address=_optional_text(detail_payload.get("address")),
                        location=_parse_location(
                            detail_payload.get("location"),
                            operation=detail_operation,
                        ),
                        categories=categories,
                        environment=_classify_environment(categories, name),
                        tags=tags,
                        source=SourceReference(
                            provider="amap",
                            provider_id=provider_id,
                            data_mode=self._data_mode,
                            retrieved_at=self._retrieved_at(),
                            raw_response_sha256=_payload_sha256(search_result, detail_payload),
                        ),
                    ),
                )
            except ProviderRequestError as error:
                if error.failure.category not in {
                    ProviderErrorCategory.EMPTY_RESULT,
                    ProviderErrorCategory.MISSING_FIELD,
                    ProviderErrorCategory.UNRECOVERABLE,
                }:
                    raise
                detail_failures.append(error)
        if not candidates and detail_failures:
            raise detail_failures[-1]
        return tuple(candidates)

    async def search_stays(self, request: StaySearchRequest) -> tuple[CandidateStay, ...]:
        pois = await self.search_pois(
            POISearchRequest(
                keywords=request.keywords,
                city_adcode=request.city_adcode,
                limit=request.limit,
            )
        )
        stays: list[CandidateStay] = []
        for poi in pois:
            classification_text = " ".join((poi.name, *poi.categories))
            if not any(term in classification_text for term in HOTEL_TERMS):
                continue
            provider_id = poi.source.provider_id
            assert provider_id is not None
            category_tags = tuple(f"category:{item}" for item in poi.categories)
            stays.append(
                CandidateStay(
                    candidate_id=f"amap-stay-{provider_id.casefold()}",
                    name=poi.name,
                    city=poi.city,
                    district=poi.district,
                    address=poi.address,
                    location=poi.location,
                    area_name=poi.district or poi.city,
                    tags=category_tags,
                    source=poi.source,
                )
            )
        if not stays:
            raise _provider_error(
                "search_stays",
                ProviderErrorCategory.EMPTY_RESULT,
                "POI search returned no hotel-classified candidates",
            )
        return tuple(stays)

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        rest_payload = await self._invoke(
            "weather_rest_fallback",
            lambda: self._client.fetch_weather_freshness(request.city_adcode),
        )
        mcp_payload = await self._invoke(
            "maps_weather",
            lambda: self._client.call_tool(
                "maps_weather",
                {"city": request.city_adcode},
            ),
        )
        rest_forecasts = _as_records(
            rest_payload.get("forecasts"),
            operation="weather_rest_fallback",
            field="forecasts",
        )
        rest_forecast = rest_forecasts[0]
        adcode = _required_text(
            rest_forecast,
            "adcode",
            operation="weather_rest_fallback",
        )
        if adcode != request.city_adcode:
            raise _provider_error(
                "weather_rest_fallback",
                ProviderErrorCategory.MISSING_FIELD,
                "weather freshness adcode does not match the request",
            )
        reporttime = _parse_reporttime(rest_forecast.get("reporttime"))
        city = _optional_text(mcp_payload.get("city")) or _required_text(
            rest_forecast,
            "city",
            operation="weather_rest_fallback",
        )
        forecasts = _as_records(
            mcp_payload.get("forecasts"),
            operation="maps_weather",
            field="forecasts",
        )
        rest_casts = _as_records(
            rest_forecast.get("casts"),
            operation="weather_rest_fallback",
            field="casts",
        )
        rest_dates = {
            _required_text(item, "date", operation="weather_rest_fallback") for item in rest_casts
        }
        source_hash = _payload_sha256(mcp_payload, rest_payload)
        risks: list[WeatherRisk] = []
        for forecast in forecasts:
            forecast_date_text = _required_text(forecast, "date", operation="maps_weather")
            if forecast_date_text not in rest_dates:
                raise _provider_error(
                    "maps_weather",
                    ProviderErrorCategory.MISSING_FIELD,
                    "MCP and REST weather forecasts do not cover the same dates",
                )
            try:
                forecast_date = date.fromisoformat(forecast_date_text)
            except ValueError as exc:
                raise _provider_error(
                    "maps_weather",
                    ProviderErrorCategory.MISSING_FIELD,
                    "maps_weather forecast date is invalid",
                ) from exc
            starts_at = datetime.combine(forecast_date, time.min, tzinfo=BEIJING_TIMEZONE)
            for risk_type, severity, metrics, threshold, advisory in _risk_specs(forecast):
                risks.append(
                    WeatherRisk(
                        risk_id=(f"amap-weather-{adcode}-{forecast_date_text}-{risk_type.value}"),
                        city=city,
                        starts_at=starts_at,
                        ends_at=starts_at + timedelta(days=1),
                        risk_type=risk_type,
                        severity=severity,
                        metrics=metrics,
                        threshold_description=threshold,
                        affected_activity_types=("outdoor",),
                        advisory=advisory,
                        source=SourceReference(
                            provider="amap",
                            provider_id=f"{adcode}:{forecast_date_text}",
                            data_mode=self._data_mode,
                            retrieved_at=reporttime,
                            raw_response_sha256=source_hash,
                        ),
                    )
                )
        return tuple(risks)

    async def get_route(self, request: RouteRequest) -> RouteLeg:
        origin = _coordinate(request.origin.location)
        destination = _coordinate(request.destination.location)
        if request.mode == RouteMode.WALKING:
            operation = "maps_direction_walking"
            arguments = {"origin": origin, "destination": destination}
        elif request.mode == RouteMode.TRANSIT:
            operation = "maps_direction_transit_integrated"
            arguments = {
                "origin": origin,
                "destination": destination,
                "city": request.city_adcode,
                "cityd": request.city_adcode,
            }
        else:
            raise _provider_error(
                "get_route",
                ProviderErrorCategory.UNRECOVERABLE,
                f"AMap V1 adapter does not support route mode {request.mode.value}",
            )

        payload = await self._invoke(
            operation,
            lambda: self._client.call_tool(operation, arguments),
        )
        if request.mode == RouteMode.WALKING:
            route = _as_object(payload.get("route"), operation=operation, field="route")
            paths = _as_records(route.get("paths"), operation=operation, field="paths")
            selected = paths[0]
        else:
            route = payload
            transits = _as_records(payload.get("transits"), operation=operation, field="transits")
            selected = transits[0]

        returned_origin = _optional_text(route.get("origin"))
        returned_destination = _optional_text(route.get("destination"))
        if returned_origin != origin or returned_destination != destination:
            raise _provider_error(
                operation,
                ProviderErrorCategory.MISSING_FIELD,
                f"{operation} response endpoints do not match the request",
            )
        distance_meters = _positive_int(
            selected.get("distance", route.get("distance")),
            operation=operation,
            field="distance",
        )
        duration_seconds = _positive_int(
            selected.get("duration"),
            operation=operation,
            field="duration",
        )
        response_hash = _payload_sha256(payload)
        route_id_hash = hashlib.sha256(
            f"{request.mode.value}:{origin}:{destination}:{response_hash}".encode()
        ).hexdigest()[:16]
        return RouteLeg(
            route_leg_id=f"amap-route-{request.mode.value}-{route_id_hash}",
            origin=request.origin,
            destination=request.destination,
            mode=request.mode,
            distance_meters=distance_meters,
            duration_minutes=math.ceil(duration_seconds / 60),
            source=SourceReference(
                provider="amap",
                provider_id=operation,
                data_mode=self._data_mode,
                retrieved_at=self._retrieved_at(),
                raw_response_sha256=response_hash,
            ),
        )


def load_fixture_amap_provider(
    fixture_path: Path = DEFAULT_AMAP_FIXTURE_PATH,
    *,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> AmapTravelDataProvider:
    return AmapTravelDataProvider(
        AmapFixtureToolClient(fixture_path),
        data_mode=DataMode.FIXTURE,
        retry_policy=retry_policy,
    )


@asynccontextmanager
async def open_live_amap_provider(
    settings: Settings,
    *,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> AsyncIterator[AmapTravelDataProvider]:
    async with AmapLiveToolClient(settings) as client:
        yield AmapTravelDataProvider(
            client,
            data_mode=DataMode.LIVE,
            retry_policy=retry_policy,
        )
