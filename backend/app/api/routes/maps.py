from typing import Annotated, Protocol, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.maps import (
    MapCoordinate,
    StaticMapConfigurationError,
    StaticMapImage,
    StaticMapProviderError,
    parse_map_coordinate,
)

router = APIRouter(prefix="/maps", tags=["maps"])


class StaticMapService(Protocol):
    async def render_plan_map(
        self,
        *,
        poi_points: tuple[MapCoordinate, ...],
        stay_point: MapCoordinate | None,
    ) -> StaticMapImage: ...


def _service(request: Request) -> StaticMapService:
    service = getattr(request.app.state, "static_map_service", None)
    if service is None or not hasattr(service, "render_plan_map"):
        raise RuntimeError("static map service is not configured")
    return cast(StaticMapService, service)


@router.get(
    "/static-plan",
    response_class=Response,
    responses={
        200: {"content": {"image/png": {}}},
        409: {"description": "AMap key is not configured"},
        422: {"description": "Coordinates are invalid"},
        502: {"description": "AMap did not return a map image"},
    },
)
async def render_static_plan_map(
    request: Request,
    poi: Annotated[list[str], Query(min_length=1, max_length=9)],
    stay: Annotated[str | None, Query()] = None,
) -> Response:
    try:
        poi_points = tuple(parse_map_coordinate(item) for item in poi)
        stay_point = parse_map_coordinate(stay) if stay is not None else None
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error_code": "static-map-invalid-coordinate", "message": str(error)},
        ) from error
    try:
        image = await _service(request).render_plan_map(
            poi_points=poi_points,
            stay_point=stay_point,
        )
    except StaticMapConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "static-map-configuration",
                "message": "服务端尚未配置高德 Key, 无法加载真实地图底图。",
            },
        ) from error
    except StaticMapProviderError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error_code": "static-map-provider",
                "message": "高德静态地图暂时不可用, 请稍后重试。",
            },
        ) from error
    return Response(
        content=image.content,
        media_type=image.media_type,
        headers={"Cache-Control": "private, max-age=300"},
    )
