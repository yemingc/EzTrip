import asyncio
import importlib.metadata
import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlencode

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import AwareDatetime, Field, JsonValue, model_validator

from app.core.config import Settings
from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.provider import ProviderErrorCategory, ProviderFailure
from app.observability.redaction import TraceRedactor

PROBE_CITY_ADCODE = "110000"
PROBE_CITY_NAME = "北京市"
PROBE_POI_KEYWORDS = ("故宫博物院", "天坛公园")
PROBE_CAPTURE_ID = "amap_mcp_beijing_20260820_v1"

REQUIRED_MCP_TOOLS = frozenset(
    {
        "maps_text_search",
        "maps_search_detail",
        "maps_weather",
        "maps_distance",
        "maps_direction_walking",
        "maps_direction_transit_integrated",
    }
)

AUTHENTICATION_INFOCODES = frozenset(
    {
        "10001",
        "10002",
        "10005",
        "10006",
        "10007",
        "10009",
        "10012",
        "10013",
        "10041",
    }
)
RATE_LIMIT_INFOCODES = frozenset(
    {
        "10003",
        "10004",
        "10010",
        "10014",
        "10019",
        "10020",
        "10021",
        "10029",
        "10044",
        "10045",
    }
)
TIMEOUT_INFOCODES = frozenset({"10015", "10016"})

SEARCH_POI_FIELDS = ("id", "name", "address", "typecode")
DETAIL_FIELDS = (
    "id",
    "name",
    "location",
    "address",
    "business_area",
    "city",
    "type",
    "alias",
    "cost",
    "open_time",
    "opentime2",
    "rating",
    "level",
    "ticket_ordering",
)
WEATHER_CAST_FIELDS = (
    "date",
    "week",
    "dayweather",
    "nightweather",
    "daytemp",
    "nighttemp",
    "daywind",
    "nightwind",
    "daypower",
    "nightpower",
    "daytemp_float",
    "nighttemp_float",
)
WALKING_STEP_FIELDS = ("instruction", "orientation", "road", "distance", "duration")


class AmapProbeTool(DomainModel):
    name: NonEmptyText
    required: tuple[NonEmptyText, ...] = ()
    properties: tuple[NonEmptyText, ...] = ()


class AmapProbeCall(DomainModel):
    operation: NonEmptyText
    transport: Literal["mcp_streamable_http", "rest_fallback"]
    arguments: dict[str, JsonValue]
    latency_ms: int = Field(ge=0)
    response: dict[str, JsonValue]

    @model_validator(mode="after")
    def reject_secret_parameters(self) -> "AmapProbeCall":
        forbidden_keys = {"key", "api_key", "token", "authorization"}
        if forbidden_keys & {key.casefold() for key in self.arguments}:
            raise ValueError("probe call arguments must not contain credentials")
        return self


class AmapProbeCapture(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    capture_id: Identifier
    captured_at: AwareDatetime
    data_mode: Literal["live_capture"] = "live_capture"
    endpoint: NonEmptyText
    mcp_sdk_version: NonEmptyText
    protocol_version: NonEmptyText
    server_name: NonEmptyText
    server_version: NonEmptyText
    tool_catalog: tuple[AmapProbeTool, ...]
    calls: tuple[AmapProbeCall, ...] = Field(min_length=1)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_capture_boundary(self) -> "AmapProbeCapture":
        if "?" in self.endpoint:
            raise ValueError("captured endpoint must not contain query parameters")
        catalog_names = {tool.name for tool in self.tool_catalog}
        missing_tools = REQUIRED_MCP_TOOLS - catalog_names
        if missing_tools:
            missing = ", ".join(sorted(missing_tools))
            raise ValueError(f"capture is missing required MCP tools: {missing}")
        return self


class McpSessionLike(Protocol):
    async def list_tools(self) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class AmapProbeError(RuntimeError):
    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def classify_amap_infocode(infocode: str) -> ProviderErrorCategory:
    if infocode in AUTHENTICATION_INFOCODES:
        return ProviderErrorCategory.AUTHENTICATION_FAILED
    if infocode in RATE_LIMIT_INFOCODES:
        return ProviderErrorCategory.RATE_LIMITED
    if infocode in TIMEOUT_INFOCODES:
        return ProviderErrorCategory.TIMEOUT
    return ProviderErrorCategory.UNRECOVERABLE


def build_amap_failure(
    *,
    operation: str,
    category: ProviderErrorCategory,
    message: str,
) -> ProviderFailure:
    return ProviderFailure(
        provider="amap",
        operation=operation,
        category=category,
        message=message,
        retryable=category in {ProviderErrorCategory.TIMEOUT, ProviderErrorCategory.RATE_LIMITED},
    )


def _as_object(value: object, *, operation: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} did not return a JSON object",
            )
        )
    return {str(key): item for key, item in value.items()}


def _as_list(value: object, *, operation: str, field: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} response field {field} is not a list",
            )
        )
    return list(value)


def _require_non_empty_records(
    payload: Mapping[str, Any],
    *,
    operation: str,
    field: str,
) -> list[Any]:
    records = _as_list(payload.get(field), operation=operation, field=field)
    if not records:
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.EMPTY_RESULT,
                message=f"{operation} returned no {field}",
            )
        )
    return records


def _select_fields(payload: Mapping[str, Any], fields: Sequence[str]) -> dict[str, JsonValue]:
    selected: dict[str, JsonValue] = {}
    for field in fields:
        if field in payload:
            selected[field] = cast(JsonValue, payload[field])
    return selected


def decode_mcp_json(result: object, *, operation: str) -> dict[str, Any]:
    content = getattr(result, "content", None)
    if not isinstance(content, Sequence):
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} returned no MCP content blocks",
            )
        )

    text: str | None = None
    for block in content:
        candidate = getattr(block, "text", None)
        if isinstance(candidate, str):
            text = candidate
            break
    if text is None:
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} returned no JSON text content",
            )
        )
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.UNRECOVERABLE,
                message=f"{operation} returned invalid JSON content",
            )
        ) from exc

    payload = _as_object(decoded, operation=operation)
    infocode = str(payload.get("infocode", ""))
    if payload.get("status") == "0" or (infocode and infocode != "10000"):
        info = str(payload.get("info", "AMap request failed"))
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=classify_amap_infocode(infocode),
                message=f"{operation} failed with AMap infocode {infocode}: {info}",
            )
        )
    if bool(getattr(result, "isError", False)):
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.UNRECOVERABLE,
                message=f"{operation} returned an MCP tool error",
            )
        )
    return payload


def sanitize_text_search(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    operation = "maps_text_search"
    pois = _require_non_empty_records(payload, operation=operation, field="pois")
    selected_pois = [
        _select_fields(_as_object(poi, operation=operation), SEARCH_POI_FIELDS) for poi in pois[:3]
    ]
    return {"pois": cast(JsonValue, selected_pois)}


def sanitize_detail(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    operation = "maps_search_detail"
    required = {"id", "name", "location", "city", "type"}
    missing = required - set(payload)
    if missing:
        fields = ", ".join(sorted(missing))
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} is missing required fields: {fields}",
            )
        )
    return _select_fields(payload, DETAIL_FIELDS)


def sanitize_mcp_weather(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    operation = "maps_weather"
    forecasts = _require_non_empty_records(payload, operation=operation, field="forecasts")
    selected_forecasts = [
        _select_fields(_as_object(forecast, operation=operation), WEATHER_CAST_FIELDS)
        for forecast in forecasts
    ]
    return {
        "city": cast(JsonValue, payload.get("city", PROBE_CITY_NAME)),
        "forecasts": cast(JsonValue, selected_forecasts),
    }


def sanitize_rest_weather(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    operation = "weather_rest_fallback"
    infocode = str(payload.get("infocode", ""))
    if payload.get("status") != "1" or infocode != "10000":
        info = str(payload.get("info", "AMap REST weather request failed"))
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=classify_amap_infocode(infocode),
                message=f"{operation} failed with AMap infocode {infocode}: {info}",
            )
        )
    forecasts = _require_non_empty_records(payload, operation=operation, field="forecasts")
    selected_forecasts: list[dict[str, JsonValue]] = []
    for forecast_value in forecasts:
        forecast = _as_object(forecast_value, operation=operation)
        required = {"adcode", "reporttime", "casts"}
        missing = required - set(forecast)
        if missing:
            fields = ", ".join(sorted(missing))
            raise AmapProbeError(
                build_amap_failure(
                    operation=operation,
                    category=ProviderErrorCategory.MISSING_FIELD,
                    message=f"{operation} is missing required fields: {fields}",
                )
            )
        casts = _as_list(forecast["casts"], operation=operation, field="casts")
        selected_forecasts.append(
            {
                **_select_fields(forecast, ("province", "city", "adcode", "reporttime")),
                "casts": cast(
                    JsonValue,
                    [
                        _select_fields(
                            _as_object(cast_value, operation=operation), WEATHER_CAST_FIELDS
                        )
                        for cast_value in casts
                    ],
                ),
            }
        )
    return {
        "status": cast(JsonValue, payload.get("status")),
        "info": cast(JsonValue, payload.get("info")),
        "infocode": cast(JsonValue, payload.get("infocode")),
        "forecasts": cast(JsonValue, selected_forecasts),
    }


def sanitize_distance(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    operation = "maps_distance"
    results = _require_non_empty_records(payload, operation=operation, field="results")
    selected = [
        _select_fields(
            _as_object(result, operation=operation),
            ("origin_id", "dest_id", "distance", "duration"),
        )
        for result in results
    ]
    return {"results": cast(JsonValue, selected)}


def sanitize_walking(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    operation = "maps_direction_walking"
    route = _as_object(payload.get("route"), operation=operation)
    paths = _require_non_empty_records(route, operation=operation, field="paths")
    selected_paths: list[dict[str, JsonValue]] = []
    for path_value in paths[:2]:
        path = _as_object(path_value, operation=operation)
        steps = _as_list(path.get("steps", []), operation=operation, field="steps")
        selected_paths.append(
            {
                **_select_fields(path, ("distance", "duration")),
                "step_count": len(steps),
                "steps": cast(
                    JsonValue,
                    [
                        _select_fields(_as_object(step, operation=operation), WALKING_STEP_FIELDS)
                        for step in steps[:3]
                    ],
                ),
            }
        )
    return {
        "route": cast(
            JsonValue,
            {
                **_select_fields(route, ("origin", "destination")),
                "paths": selected_paths,
            },
        )
    }


def sanitize_transit(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    operation = "maps_direction_transit_integrated"
    transits = _require_non_empty_records(payload, operation=operation, field="transits")
    selected_transits: list[dict[str, JsonValue]] = []
    for transit_value in transits[:2]:
        transit = _as_object(transit_value, operation=operation)
        segments = _as_list(transit.get("segments", []), operation=operation, field="segments")
        selected_transits.append(
            {
                **_select_fields(transit, ("cost", "duration", "walking_distance", "nightflag")),
                "segment_count": len(segments),
            }
        )
    return {
        **_select_fields(payload, ("origin", "destination", "distance")),
        "transits": cast(JsonValue, selected_transits),
    }


async def _timed_mcp_call(
    session: McpSessionLike,
    *,
    operation: str,
    arguments: dict[str, Any],
    sanitizer: Any,
) -> tuple[AmapProbeCall, dict[str, Any]]:
    started = time.perf_counter()
    result = await session.call_tool(operation, arguments)
    latency_ms = round((time.perf_counter() - started) * 1000)
    payload = decode_mcp_json(result, operation=operation)
    sanitized = sanitizer(payload)
    return (
        AmapProbeCall(
            operation=operation,
            transport="mcp_streamable_http",
            arguments=cast(dict[str, JsonValue], arguments),
            latency_ms=latency_ms,
            response=sanitized,
        ),
        payload,
    )


def _first_poi_id(payload: Mapping[str, Any], *, operation: str) -> str:
    pois = _require_non_empty_records(payload, operation=operation, field="pois")
    first = _as_object(pois[0], operation=operation)
    poi_id = first.get("id")
    if not isinstance(poi_id, str) or not poi_id:
        raise AmapProbeError(
            build_amap_failure(
                operation=operation,
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"{operation} first POI has no id",
            )
        )
    return poi_id


def _detail_location(payload: Mapping[str, Any]) -> str:
    location = payload.get("location")
    if not isinstance(location, str) or "," not in location:
        raise AmapProbeError(
            build_amap_failure(
                operation="maps_search_detail",
                category=ProviderErrorCategory.MISSING_FIELD,
                message="maps_search_detail has no usable location",
            )
        )
    return location


def parse_tool_catalog(result: object) -> tuple[AmapProbeTool, ...]:
    tools = getattr(result, "tools", None)
    if not isinstance(tools, Sequence):
        raise AmapProbeError(
            build_amap_failure(
                operation="list_tools",
                category=ProviderErrorCategory.MISSING_FIELD,
                message="AMap MCP list_tools returned no tools",
            )
        )
    catalog: list[AmapProbeTool] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        input_schema = getattr(tool, "inputSchema", {})
        if not isinstance(name, str) or not isinstance(input_schema, Mapping):
            continue
        required = input_schema.get("required", [])
        properties = input_schema.get("properties", {})
        catalog.append(
            AmapProbeTool(
                name=name,
                required=tuple(str(item) for item in required)
                if isinstance(required, Sequence)
                else (),
                properties=(
                    tuple(sorted(str(key) for key in properties))
                    if isinstance(properties, Mapping)
                    else ()
                ),
            )
        )
    return tuple(catalog)


async def fetch_rest_weather(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[dict[str, JsonValue], int]:
    secret = settings.amap_maps_api_key
    if secret is None:
        raise AmapProbeError(
            build_amap_failure(
                operation="weather_rest_fallback",
                category=ProviderErrorCategory.AUTHENTICATION_FAILED,
                message="AMAP_MAPS_API_KEY is required for the live AMap probe",
            )
        )
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=settings.amap_mcp_timeout_seconds)
    started = time.perf_counter()
    try:
        response = await active_client.get(
            settings.amap_rest_weather_url,
            params={
                "key": secret.get_secret_value(),
                "city": PROBE_CITY_ADCODE,
                "extensions": "all",
                "output": "JSON",
            },
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code == 429:
            raise AmapProbeError(
                build_amap_failure(
                    operation="weather_rest_fallback",
                    category=ProviderErrorCategory.RATE_LIMITED,
                    message="AMap REST weather preflight returned HTTP 429",
                )
            )
        if response.status_code >= 400:
            raise AmapProbeError(
                build_amap_failure(
                    operation="weather_rest_fallback",
                    category=ProviderErrorCategory.UNRECOVERABLE,
                    message=f"AMap REST weather preflight returned HTTP {response.status_code}",
                )
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise AmapProbeError(
                build_amap_failure(
                    operation="weather_rest_fallback",
                    category=ProviderErrorCategory.UNRECOVERABLE,
                    message="AMap REST weather preflight returned invalid JSON",
                )
            ) from exc
        sanitized = sanitize_rest_weather(_as_object(payload, operation="weather_rest_fallback"))
        return sanitized, latency_ms
    except httpx.TimeoutException as exc:
        raise AmapProbeError(
            build_amap_failure(
                operation="weather_rest_fallback",
                category=ProviderErrorCategory.TIMEOUT,
                message="AMap REST weather preflight timed out",
            )
        ) from exc
    except httpx.RequestError as exc:
        raise AmapProbeError(
            build_amap_failure(
                operation="weather_rest_fallback",
                category=ProviderErrorCategory.UNRECOVERABLE,
                message="AMap REST weather preflight failed before receiving a response",
            )
        ) from exc
    finally:
        if owns_client:
            await active_client.aclose()


async def collect_amap_probe(
    session: McpSessionLike,
    *,
    endpoint: str,
    server_name: str,
    server_version: str,
    protocol_version: str,
    rest_weather: dict[str, JsonValue],
    rest_weather_latency_ms: int,
    captured_at: datetime,
) -> AmapProbeCapture:
    catalog = parse_tool_catalog(await session.list_tools())
    missing_tools = REQUIRED_MCP_TOOLS - {tool.name for tool in catalog}
    if missing_tools:
        missing = ", ".join(sorted(missing_tools))
        raise AmapProbeError(
            build_amap_failure(
                operation="list_tools",
                category=ProviderErrorCategory.MISSING_FIELD,
                message=f"AMap MCP is missing required tools: {missing}",
            )
        )

    calls: list[AmapProbeCall] = [
        AmapProbeCall(
            operation="weather_rest_fallback",
            transport="rest_fallback",
            arguments={"city": PROBE_CITY_ADCODE, "extensions": "all"},
            latency_ms=rest_weather_latency_ms,
            response=rest_weather,
        )
    ]
    details: list[dict[str, Any]] = []
    for keyword in PROBE_POI_KEYWORDS:
        search_call, search_payload = await _timed_mcp_call(
            session,
            operation="maps_text_search",
            arguments={"keywords": keyword, "city": PROBE_CITY_ADCODE, "citylimit": True},
            sanitizer=sanitize_text_search,
        )
        calls.append(search_call)
        detail_call, detail_payload = await _timed_mcp_call(
            session,
            operation="maps_search_detail",
            arguments={"id": _first_poi_id(search_payload, operation="maps_text_search")},
            sanitizer=sanitize_detail,
        )
        calls.append(detail_call)
        details.append(detail_payload)

    weather_call, _ = await _timed_mcp_call(
        session,
        operation="maps_weather",
        arguments={"city": PROBE_CITY_ADCODE},
        sanitizer=sanitize_mcp_weather,
    )
    calls.append(weather_call)

    origin = _detail_location(details[0])
    destination = _detail_location(details[1])
    distance_call, _ = await _timed_mcp_call(
        session,
        operation="maps_distance",
        arguments={"origins": origin, "destination": destination, "type": "1"},
        sanitizer=sanitize_distance,
    )
    calls.append(distance_call)
    walking_call, _ = await _timed_mcp_call(
        session,
        operation="maps_direction_walking",
        arguments={"origin": origin, "destination": destination},
        sanitizer=sanitize_walking,
    )
    calls.append(walking_call)
    transit_call, _ = await _timed_mcp_call(
        session,
        operation="maps_direction_transit_integrated",
        arguments={
            "origin": origin,
            "destination": destination,
            "city": PROBE_CITY_ADCODE,
            "cityd": PROBE_CITY_ADCODE,
        },
        sanitizer=sanitize_transit,
    )
    calls.append(transit_call)

    return AmapProbeCapture(
        capture_id=PROBE_CAPTURE_ID,
        captured_at=captured_at,
        endpoint=endpoint,
        mcp_sdk_version=importlib.metadata.version("mcp"),
        protocol_version=protocol_version,
        server_name=server_name,
        server_version=server_version,
        tool_catalog=catalog,
        calls=tuple(calls),
        limitations=(
            "Live capture is a point-in-time contract fixture, not current travel advice.",
            "MCP weather omits provider reporttime and adcode; the REST weather call is retained "
            "as a freshness fallback.",
            "POI cost and opening fields can be empty or irregular and must not be treated as "
            "booking inventory.",
        ),
    )


def build_mcp_url(endpoint: str, key: str) -> str:
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode({'key': key})}"


async def run_live_amap_probe(settings: Settings) -> AmapProbeCapture:
    secret = settings.amap_maps_api_key
    if secret is None:
        raise AmapProbeError(
            build_amap_failure(
                operation="initialize",
                category=ProviderErrorCategory.AUTHENTICATION_FAILED,
                message="AMAP_MAPS_API_KEY is required for the live AMap probe",
            )
        )
    rest_weather, rest_latency = await fetch_rest_weather(settings)
    url = build_mcp_url(settings.amap_mcp_url, secret.get_secret_value())
    try:
        async with asyncio.timeout(settings.amap_probe_total_timeout_seconds):
            async with httpx.AsyncClient(timeout=settings.amap_mcp_timeout_seconds) as client:
                async with streamable_http_client(url, http_client=client) as (
                    read_stream,
                    write_stream,
                    _,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        return await collect_amap_probe(
                            session,
                            endpoint=settings.amap_mcp_url,
                            server_name=initialized.serverInfo.name,
                            server_version=initialized.serverInfo.version,
                            protocol_version=str(initialized.protocolVersion),
                            rest_weather=rest_weather,
                            rest_weather_latency_ms=rest_latency,
                            captured_at=datetime.now(UTC),
                        )
    except TimeoutError as exc:
        raise AmapProbeError(
            build_amap_failure(
                operation="amap_mcp_probe",
                category=ProviderErrorCategory.TIMEOUT,
                message="AMap MCP live probe exceeded its total timeout",
            )
        ) from exc
    except AmapProbeError:
        raise
    except Exception as exc:
        safe_message = TraceRedactor.from_settings(settings).redact_text(str(exc))
        raise AmapProbeError(
            build_amap_failure(
                operation="amap_mcp_probe",
                category=ProviderErrorCategory.UNRECOVERABLE,
                message=f"AMap MCP live probe failed: {safe_message}",
            )
        ) from exc


def write_probe_capture(
    capture: AmapProbeCapture,
    output_path: Path,
    *,
    redactor: TraceRedactor,
) -> None:
    payload = capture.model_dump(mode="json")
    redacted = redactor.redact_value(payload)
    if not isinstance(redacted, dict):
        raise TypeError("AMap probe capture must remain a dictionary after redaction")
    serialized = json.dumps(redacted, ensure_ascii=False, indent=2) + "\n"
    for secret in redactor.secrets:
        if secret and secret in serialized:
            raise ValueError("AMap probe fixture still contains a configured secret")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialized, encoding="utf-8")
