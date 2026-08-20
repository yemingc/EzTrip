import argparse
import asyncio
import json
from typing import Any

from app.core.config import Settings
from app.domain.travel_data import RouteEndpoint, RouteMode
from app.observability.redaction import TraceRedactor
from app.providers import (
    POISearchRequest,
    ProviderRequestError,
    RouteRequest,
    TravelDataProvider,
    WeatherRiskRequest,
    load_fixture_amap_provider,
    open_live_amap_provider,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the fixed Beijing provider adapter smoke scenario."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the configured AMap Key and network. The default replays the fixture.",
    )
    return parser.parse_args()


async def invoke_provider(provider: TravelDataProvider) -> dict[str, Any]:
    palace = (
        await provider.search_pois(POISearchRequest(keywords="故宫博物院", city_adcode="110000"))
    )[0]
    temple = (
        await provider.search_pois(POISearchRequest(keywords="天坛公园", city_adcode="110000"))
    )[0]
    risks = await provider.get_weather_risks(WeatherRiskRequest(city_adcode="110000"))
    origin = RouteEndpoint(
        name=palace.name,
        candidate_id=palace.candidate_id,
        location=palace.location,
    )
    destination = RouteEndpoint(
        name=temple.name,
        candidate_id=temple.candidate_id,
        location=temple.location,
    )
    routes = [
        await provider.get_route(
            RouteRequest(
                origin=origin,
                destination=destination,
                mode=mode,
                city_adcode="110000",
            )
        )
        for mode in (RouteMode.WALKING, RouteMode.TRANSIT)
    ]
    return {
        "status": "ok",
        "data_mode": palace.source.data_mode,
        "pois": [
            {
                "candidate_id": poi.candidate_id,
                "name": poi.name,
                "environment": poi.environment,
                "source_provider_id": poi.source.provider_id,
                "has_response_hash": poi.source.raw_response_sha256 is not None,
            }
            for poi in (palace, temple)
        ],
        "weather_risks": [
            {
                "date": risk.starts_at.date().isoformat(),
                "type": risk.risk_type,
                "severity": risk.severity,
                "retrieved_at": risk.source.retrieved_at.isoformat(),
            }
            for risk in risks
        ],
        "routes": [
            {
                "mode": route.mode,
                "distance_meters": route.distance_meters,
                "duration_minutes": route.duration_minutes,
                "has_response_hash": route.source.raw_response_sha256 is not None,
            }
            for route in routes
        ],
        "raw_payload_printed": False,
    }


async def run(*, live: bool) -> dict[str, Any]:
    if not live:
        return await invoke_provider(load_fixture_amap_provider())
    settings = Settings()
    async with open_live_amap_provider(settings) as provider:
        return await invoke_provider(provider)


def main() -> int:
    args = parse_args()
    settings = Settings()
    redactor = TraceRedactor.from_settings(settings)
    try:
        summary = asyncio.run(run(live=args.live))
        print(json.dumps(summary, ensure_ascii=True, indent=2))
        return 0
    except ProviderRequestError as exc:
        failure = redactor.redact_value(exc.failure.model_dump(mode="json"))
        print(json.dumps({"status": "error", "failure": failure}, ensure_ascii=True))
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": redactor.redact_text(str(exc)),
                },
                ensure_ascii=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
