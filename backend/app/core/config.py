from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_prefix="EZTRIP_",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "EzTrip API"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://eztrip:eztrip-local-only@localhost:55432/eztrip"
    cors_origins: list[str] = ["http://localhost:3000"]
    llm_provider: Literal["deepseek"] = "deepseek"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 1
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="DEEPSEEK_API_KEY",
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias="DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-pro",
        validation_alias="DEEPSEEK_MODEL",
    )
    langsmith_tracing: bool = Field(
        default=False,
        validation_alias="LANGSMITH_TRACING",
    )
    langsmith_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="LANGSMITH_API_KEY",
    )
    langsmith_endpoint: str = Field(
        default="https://api.smith.langchain.com",
        validation_alias="LANGSMITH_ENDPOINT",
    )
    langsmith_project: str = Field(
        default="eztrip-dev",
        validation_alias="LANGSMITH_PROJECT",
    )
    langsmith_workspace_id: str | None = Field(
        default=None,
        validation_alias="LANGSMITH_WORKSPACE_ID",
    )
    amap_maps_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="AMAP_MAPS_API_KEY",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
