import asyncio
import json
from collections.abc import Mapping
from contextlib import AsyncExitStack
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from app.core.config import Settings
from app.domain.provider import ProviderErrorCategory
from app.observability.redaction import TraceRedactor
from app.providers.amap_probe import AmapProbeCapture
from app.providers.amap_protocol import (
    build_amap_failure,
    build_mcp_url,
    classify_amap_infocode,
    decode_mcp_json,
)
from app.providers.errors import ProviderRequestError

DEFAULT_AMAP_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "evals"
    / "fixtures"
    / "amap"
    / "mcp-beijing-2026-08-20.v1.json"
)


class AmapToolClient(Protocol):
    captured_at: datetime | None

    async def call_tool(self, operation: str, arguments: dict[str, Any]) -> dict[str, object]: ...

    async def fetch_weather_freshness(self, city_adcode: str) -> dict[str, object]: ...


def _canonical_request(operation: str, arguments: Mapping[str, object]) -> str:
    return json.dumps(
        {"operation": operation, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class AmapFixtureToolClient:
    """Replay allow-listed provider responses without a Key or network access."""

    def __init__(self, fixture_path: Path = DEFAULT_AMAP_FIXTURE_PATH) -> None:
        try:
            capture = AmapProbeCapture.model_validate_json(fixture_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="load_fixture",
                    category=ProviderErrorCategory.UNRECOVERABLE,
                    message="AMap fixture could not be loaded or did not match its contract",
                )
            ) from exc

        self.captured_at: datetime | None = capture.captured_at
        self._responses: dict[str, dict[str, object]] = {}
        for call in capture.calls:
            key = _canonical_request(call.operation, call.arguments)
            if key in self._responses:
                raise ProviderRequestError(
                    build_amap_failure(
                        operation="load_fixture",
                        category=ProviderErrorCategory.UNRECOVERABLE,
                        message="AMap fixture contains duplicate operation arguments",
                    )
                )
            response: dict[str, object] = {
                field: deepcopy(value) for field, value in call.response.items()
            }
            self._responses[key] = response

    async def call_tool(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        key = _canonical_request(operation, arguments)
        response = self._responses.get(key)
        if response is None:
            raise ProviderRequestError(
                build_amap_failure(
                    operation=operation,
                    category=ProviderErrorCategory.EMPTY_RESULT,
                    message=f"fixture has no recorded response for {operation}",
                )
            )
        return deepcopy(response)

    async def fetch_weather_freshness(self, city_adcode: str) -> dict[str, object]:
        return await self.call_tool(
            "weather_rest_fallback",
            {"city": city_adcode, "extensions": "all"},
        )


class AmapLiveToolClient:
    """Request-scoped official MCP session with REST auth/freshness preflight."""

    captured_at: datetime | None = None

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redactor = TraceRedactor.from_settings(settings)
        self._stack: AsyncExitStack | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._session: ClientSession | None = None
        self._weather_freshness: dict[str, dict[str, object]] = {}

    async def __aenter__(self) -> "AmapLiveToolClient":
        secret = self._settings.amap_maps_api_key
        if secret is None:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="initialize",
                    category=ProviderErrorCategory.AUTHENTICATION_FAILED,
                    message="AMAP_MAPS_API_KEY is required for the live AMap adapter",
                )
            )

        stack = AsyncExitStack()
        await stack.__aenter__()
        self._stack = stack
        try:
            client = await stack.enter_async_context(
                httpx.AsyncClient(timeout=self._settings.amap_mcp_timeout_seconds)
            )
            self._http_client = client
            self._weather_freshness["110000"] = await self._fetch_weather_from_rest("110000")
            url = build_mcp_url(
                self._settings.amap_mcp_url,
                secret.get_secret_value(),
            )
            async with asyncio.timeout(self._settings.amap_mcp_timeout_seconds):
                read_stream, write_stream, _ = await stack.enter_async_context(
                    streamable_http_client(url, http_client=client)
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
            self._session = session
            return self
        except ProviderRequestError:
            await self._close()
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            await self._close()
            raise ProviderRequestError(
                build_amap_failure(
                    operation="initialize",
                    category=ProviderErrorCategory.TIMEOUT,
                    message="AMap live adapter initialization timed out",
                )
            ) from exc
        except Exception as exc:
            await self._close()
            safe_message = self._redactor.redact_text(str(exc))
            raise ProviderRequestError(
                build_amap_failure(
                    operation="initialize",
                    category=ProviderErrorCategory.UNRECOVERABLE,
                    message=f"AMap live adapter initialization failed: {safe_message}",
                )
            ) from exc

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._close()

    async def _close(self) -> None:
        stack = self._stack
        self._stack = None
        self._http_client = None
        self._session = None
        self._weather_freshness = {}
        if stack is not None:
            await stack.aclose()

    async def call_tool(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        session = self._session
        if session is None:
            raise ProviderRequestError(
                build_amap_failure(
                    operation=operation,
                    category=ProviderErrorCategory.UNRECOVERABLE,
                    message="AMap live adapter must be opened before calling tools",
                )
            )
        try:
            async with asyncio.timeout(self._settings.amap_mcp_timeout_seconds):
                result = await session.call_tool(operation, arguments)
            return decode_mcp_json(result, operation=operation)
        except ProviderRequestError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ProviderRequestError(
                build_amap_failure(
                    operation=operation,
                    category=ProviderErrorCategory.TIMEOUT,
                    message=f"{operation} timed out",
                )
            ) from exc
        except Exception as exc:
            safe_message = self._redactor.redact_text(str(exc))
            raise ProviderRequestError(
                build_amap_failure(
                    operation=operation,
                    category=ProviderErrorCategory.UNRECOVERABLE,
                    message=f"{operation} failed: {safe_message}",
                )
            ) from exc

    async def fetch_weather_freshness(self, city_adcode: str) -> dict[str, object]:
        if self._http_client is None:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="weather_rest_fallback",
                    category=ProviderErrorCategory.UNRECOVERABLE,
                    message="AMap live adapter must be opened before reading weather freshness",
                )
            )
        freshness = self._weather_freshness.get(city_adcode)
        if freshness is None:
            freshness = await self._fetch_weather_from_rest(city_adcode)
            self._weather_freshness[city_adcode] = freshness
        forecasts = freshness.get("forecasts")
        if not isinstance(forecasts, list) or not forecasts:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="weather_rest_fallback",
                    category=ProviderErrorCategory.MISSING_FIELD,
                    message="AMap weather freshness has no forecasts",
                )
            )
        forecast = forecasts[0]
        if not isinstance(forecast, Mapping) or str(forecast.get("adcode")) != city_adcode:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="weather_rest_fallback",
                    category=ProviderErrorCategory.MISSING_FIELD,
                    message="AMap weather freshness does not match the requested city",
                )
            )
        return deepcopy(freshness)

    async def _fetch_weather_from_rest(self, city_adcode: str) -> dict[str, object]:
        client = self._http_client
        secret = self._settings.amap_maps_api_key
        if client is None or secret is None:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="weather_rest_fallback",
                    category=ProviderErrorCategory.UNRECOVERABLE,
                    message="AMap REST weather client is not initialized",
                )
            )
        try:
            response = await client.get(
                self._settings.amap_rest_weather_url,
                params={
                    "key": secret.get_secret_value(),
                    "city": city_adcode,
                    "extensions": "all",
                    "output": "JSON",
                },
            )
            if response.status_code == 429:
                raise ProviderRequestError(
                    build_amap_failure(
                        operation="weather_rest_fallback",
                        category=ProviderErrorCategory.RATE_LIMITED,
                        message="AMap REST weather returned HTTP 429",
                    )
                )
            if response.status_code >= 400:
                raise ProviderRequestError(
                    build_amap_failure(
                        operation="weather_rest_fallback",
                        category=ProviderErrorCategory.UNRECOVERABLE,
                        message=f"AMap REST weather returned HTTP {response.status_code}",
                    )
                )
            try:
                decoded = response.json()
            except json.JSONDecodeError as exc:
                raise ProviderRequestError(
                    build_amap_failure(
                        operation="weather_rest_fallback",
                        category=ProviderErrorCategory.UNRECOVERABLE,
                        message="AMap REST weather returned invalid JSON",
                    )
                ) from exc
            if not isinstance(decoded, Mapping):
                raise ProviderRequestError(
                    build_amap_failure(
                        operation="weather_rest_fallback",
                        category=ProviderErrorCategory.MISSING_FIELD,
                        message="AMap REST weather did not return an object",
                    )
                )
            payload = {str(key): value for key, value in decoded.items()}
            infocode = str(payload.get("infocode", ""))
            if payload.get("status") != "1" or infocode != "10000":
                info = str(payload.get("info", "AMap REST weather request failed"))
                raise ProviderRequestError(
                    build_amap_failure(
                        operation="weather_rest_fallback",
                        category=classify_amap_infocode(infocode),
                        message=(
                            f"weather_rest_fallback failed with AMap infocode {infocode}: {info}"
                        ),
                    )
                )
            return payload
        except ProviderRequestError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="weather_rest_fallback",
                    category=ProviderErrorCategory.TIMEOUT,
                    message="AMap REST weather timed out",
                )
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderRequestError(
                build_amap_failure(
                    operation="weather_rest_fallback",
                    category=ProviderErrorCategory.UNRECOVERABLE,
                    message="AMap REST weather failed before receiving a response",
                )
            ) from exc
