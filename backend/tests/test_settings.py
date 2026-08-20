from app.core.config import Settings


def test_settings_have_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.cors_origins == ["http://localhost:3000"]
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.llm_provider == "deepseek"
    assert settings.deepseek_model == "deepseek-v4-pro"
    assert settings.langsmith_tracing is False
