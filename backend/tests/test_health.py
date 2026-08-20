import asyncio

from httpx import ASGITransport, AsyncClient, Response

from app.main import app


async def request_health() -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/health")


def test_health_returns_service_metadata() -> None:
    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "EzTrip API",
        "version": "0.1.0",
        "environment": "development",
    }
