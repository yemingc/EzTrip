from pydantic import SecretStr

from app.core.config import Settings
from app.observability.redaction import (
    REDACTED_EMAIL,
    REDACTED_PHONE,
    REDACTED_SECRET,
    TraceRedactor,
)


def test_trace_redactor_removes_configured_secrets_and_pii_recursively() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("deepseek-test-secret-value"),
        langsmith_api_key=SecretStr("langsmith-test-secret-value"),
        amap_maps_api_key=SecretStr("amap-test-secret-value"),
    )
    redactor = TraceRedactor.from_settings(settings)
    common_secret_candidate = "sk-" + "abcdefghijklmnopqrstuvwxyz"
    payload = {
        "inputs": {
            "text": (
                "联系 13800138000 或 traveler@example.com;"
                "keys=deepseek-test-secret-value/langsmith-test-secret-value/"
                "amap-test-secret-value"
            )
        },
        "items": [common_secret_candidate, "safe"],
    }

    redacted = redactor.redact_value(payload)

    assert redacted == {
        "inputs": {
            "text": (
                f"联系 {REDACTED_PHONE} 或 {REDACTED_EMAIL};"
                f"keys={REDACTED_SECRET}/{REDACTED_SECRET}/{REDACTED_SECRET}"
            )
        },
        "items": [REDACTED_SECRET, "safe"],
    }


def test_secret_settings_do_not_expose_values_in_repr() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("deepseek-test-secret-value"),
        langsmith_api_key=SecretStr("langsmith-test-secret-value"),
    )

    rendered = repr(settings)

    assert "deepseek-test-secret-value" not in rendered
    assert "langsmith-test-secret-value" not in rendered
    assert "**********" in rendered
