import asyncio

from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app
from app.maps import MapCoordinate, StaticMapImage, build_static_map_parameters


class FakeStaticMapService:
    def __init__(self) -> None:
        self.poi_points: tuple[MapCoordinate, ...] = ()
        self.stay_point: MapCoordinate | None = None

    async def render_plan_map(
        self,
        *,
        poi_points: tuple[MapCoordinate, ...],
        stay_point: MapCoordinate | None,
    ) -> StaticMapImage:
        self.poi_points = poi_points
        self.stay_point = stay_point
        return StaticMapImage(content=b"fixture-map", media_type="image/png")


def test_static_map_parameters_keep_the_key_server_side_and_label_plan_order() -> None:
    parameters = build_static_map_parameters(
        poi_points=(
            MapCoordinate(longitude=116.397029, latitude=39.917839),
            MapCoordinate(longitude=116.385121, latitude=39.941893),
        ),
        stay_point=MapCoordinate(longitude=116.470135, latitude=39.909139),
        api_key="server-only-key",
    )

    assert parameters["key"] == "server-only-key"
    assert "H:116.470135,39.909139" in parameters["markers"]
    assert "1:116.397029,39.917839" in parameters["markers"]
    assert "2:116.385121,39.941893" in parameters["markers"]
    assert parameters["paths"].endswith("116.397029,39.917839;116.385121,39.941893")


def test_static_plan_map_endpoint_proxies_an_image_without_accepting_a_key() -> None:
    async def scenario() -> None:
        application = create_app(settings=Settings(_env_file=None))
        service = FakeStaticMapService()
        application.state.static_map_service = service
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/maps/static-plan",
                params=[
                    ("poi", "116.397029,39.917839"),
                    ("poi", "116.385121,39.941893"),
                    ("stay", "116.470135,39.909139"),
                ],
            )

        assert response.status_code == 200
        assert response.content == b"fixture-map"
        assert response.headers["content-type"] == "image/png"
        assert service.poi_points[0].longitude == 116.397029
        assert service.stay_point is not None
        assert service.stay_point.latitude == 39.909139

    asyncio.run(scenario())


def test_static_plan_map_reports_missing_server_configuration() -> None:
    async def scenario() -> None:
        application = create_app(settings=Settings(_env_file=None))
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/maps/static-plan",
                params={"poi": "116.397029,39.917839"},
            )

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "static-map-configuration"

    asyncio.run(scenario())
