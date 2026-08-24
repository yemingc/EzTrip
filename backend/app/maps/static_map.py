import math
from dataclasses import dataclass

import httpx

from app.core.config import Settings


class StaticMapConfigurationError(RuntimeError):
    """Raised when a static map cannot be requested safely."""


class StaticMapProviderError(RuntimeError):
    """Raised when AMap does not return a usable map image."""


@dataclass(frozen=True, slots=True)
class MapCoordinate:
    longitude: float
    latitude: float

    def as_amap_text(self) -> str:
        return f"{self.longitude:.6f},{self.latitude:.6f}"


@dataclass(frozen=True, slots=True)
class StaticMapImage:
    content: bytes
    media_type: str


def parse_map_coordinate(value: str) -> MapCoordinate:
    parts = value.split(",")
    if len(parts) != 2:
        raise ValueError("map coordinates must use longitude,latitude")
    try:
        longitude, latitude = (float(part) for part in parts)
    except ValueError as error:
        raise ValueError("map coordinates must be numeric") from error
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise ValueError("map coordinates are outside valid geographic bounds")
    return MapCoordinate(longitude=longitude, latitude=latitude)


def _map_center(points: tuple[MapCoordinate, ...]) -> MapCoordinate:
    return MapCoordinate(
        longitude=(
            min(point.longitude for point in points) + max(point.longitude for point in points)
        )
        / 2,
        latitude=(min(point.latitude for point in points) + max(point.latitude for point in points))
        / 2,
    )


def _map_zoom(points: tuple[MapCoordinate, ...]) -> int:
    if len(points) == 1:
        return 14
    longitude_span = max(point.longitude for point in points) - min(
        point.longitude for point in points
    )
    latitude_span = max(point.latitude for point in points) - min(
        point.latitude for point in points
    )
    padded_longitude_span = max(longitude_span * 1.45, 0.005)
    padded_latitude_span = max(latitude_span * 1.45, 0.005)
    longitude_zoom = math.log2(360 * 800 / (256 * padded_longitude_span))
    latitude_zoom = math.log2(170 * 420 / (256 * padded_latitude_span))
    return max(4, min(17, math.floor(min(longitude_zoom, latitude_zoom))))


def build_static_map_parameters(
    *,
    poi_points: tuple[MapCoordinate, ...],
    stay_point: MapCoordinate | None,
    api_key: str,
) -> dict[str, str]:
    if not 1 <= len(poi_points) <= 9:
        raise ValueError("static plan maps require between one and nine POI points")
    all_points = (*((stay_point,) if stay_point is not None else ()), *poi_points)
    marker_specs: list[str] = []
    if stay_point is not None:
        marker_specs.append(f"mid,0x0F172A,H:{stay_point.as_amap_text()}")
    marker_specs.extend(
        f"mid,0x047857,{index}:{point.as_amap_text()}"
        for index, point in enumerate(poi_points, start=1)
    )
    parameters = {
        "location": _map_center(all_points).as_amap_text(),
        "zoom": str(_map_zoom(all_points)),
        "size": "800*420",
        "scale": "2",
        "markers": "|".join(marker_specs),
        "key": api_key,
    }
    if len(poi_points) > 1:
        parameters["paths"] = "4,0x047857,0.7,,0.6:" + ";".join(
            point.as_amap_text() for point in poi_points
        )
    return parameters


class AmapStaticMapService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def render_plan_map(
        self,
        *,
        poi_points: tuple[MapCoordinate, ...],
        stay_point: MapCoordinate | None,
    ) -> StaticMapImage:
        secret = self._settings.amap_maps_api_key
        if secret is None:
            raise StaticMapConfigurationError("AMAP_MAPS_API_KEY is required")
        parameters = build_static_map_parameters(
            poi_points=poi_points,
            stay_point=stay_point,
            api_key=secret.get_secret_value(),
        )
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.amap_mcp_timeout_seconds,
            ) as client:
                response = await client.get(
                    self._settings.amap_rest_static_map_url,
                    params=parameters,
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise StaticMapProviderError("AMap static map request failed") from error
        media_type = response.headers.get("content-type", "").split(";", 1)[0]
        if not media_type.startswith("image/") or not response.content:
            raise StaticMapProviderError("AMap static map response was not an image")
        return StaticMapImage(content=response.content, media_type=media_type)
