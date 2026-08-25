from pathlib import Path

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
    assert settings.planning_task_store_path == Path("tmp/planning-task-store.sqlite3")


@pytest.mark.parametrize("value", ["", "   "])
def test_optional_secret_settings_normalize_blank_values_to_none(value: str) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key=value,
        langsmith_api_key=value,
        amap_maps_api_key=value,
    )

    assert settings.deepseek_api_key is None
    assert settings.langsmith_api_key is None
    assert settings.amap_maps_api_key is None


def test_root_environment_example_is_safe_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "LANGSMITH_API_KEY",
        "AMAP_MAPS_API_KEY",
        "LANGSMITH_TRACING",
    ):
        monkeypatch.delenv(name, raising=False)

    example_path = Path(__file__).parents[2] / ".env.example"
    settings = Settings(_env_file=example_path)

    assert settings.deepseek_api_key is None
    assert settings.langsmith_api_key is None
    assert settings.amap_maps_api_key is None
    assert settings.langsmith_tracing is False


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
