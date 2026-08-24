import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_settings_have_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.cors_origins == ["http://localhost:3000"]
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.llm_provider == "deepseek"
    assert settings.deepseek_model == "deepseek-v4-pro"
    assert settings.langsmith_tracing is False
    assert settings.amap_mcp_url == "https://mcp.amap.com/mcp"
    assert settings.amap_mcp_transport == "streamable_http"
    assert settings.amap_rest_static_map_url == "https://restapi.amap.com/v3/staticmap"


@pytest.mark.parametrize(
    "field,value",
    [
        ("amap_mcp_url", "http://mcp.amap.com/mcp"),
        ("amap_mcp_url", "https://mcp.amap.com/mcp?key=must-not-live-here"),
        ("amap_rest_weather_url", "http://restapi.amap.com/weather"),
        ("amap_rest_weather_url", "https://restapi.amap.com/weather?key=unsafe"),
        ("amap_rest_static_map_url", "http://restapi.amap.com/v3/staticmap"),
        ("amap_rest_static_map_url", "https://restapi.amap.com/v3/staticmap?key=unsafe"),
    ],
)
def test_amap_endpoint_settings_reject_insecure_or_credential_bearing_urls(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match="AMap endpoint"):
        Settings(_env_file=None, **{field: value})
