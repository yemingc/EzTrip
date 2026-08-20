import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings

REDACTED_SECRET = "<redacted-secret>"
REDACTED_EMAIL = "<redacted-email>"
REDACTED_PHONE = "<redacted-phone>"

EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
CHINA_MOBILE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
COMMON_SECRET_PATTERN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|lsv2_[A-Za-z0-9_-]{16,})\b")


@dataclass(frozen=True)
class TraceRedactor:
    """Redact configured secrets and common PII before trace upload."""

    secrets: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Settings) -> "TraceRedactor":
        configured = (
            settings.deepseek_api_key,
            settings.langsmith_api_key,
            settings.amap_maps_api_key,
        )
        secrets = tuple(
            secret.get_secret_value()
            for secret in configured
            if secret is not None and secret.get_secret_value()
        )
        return cls(secrets=secrets)

    def redact_text(self, value: str) -> str:
        redacted = value
        for secret in sorted(self.secrets, key=len, reverse=True):
            redacted = redacted.replace(secret, REDACTED_SECRET)
        redacted = COMMON_SECRET_PATTERN.sub(REDACTED_SECRET, redacted)
        redacted = EMAIL_PATTERN.sub(REDACTED_EMAIL, redacted)
        return CHINA_MOBILE_PATTERN.sub(REDACTED_PHONE, redacted)

    def redact_value(self, value: object) -> object:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {str(key): self.redact_value(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [self.redact_value(item) for item in value]
        return value

    def anonymize_run(self, run: dict[str, Any]) -> dict[str, Any]:
        redacted = self.redact_value(run)
        if not isinstance(redacted, dict):
            raise TypeError("LangSmith run payload must remain a dictionary after redaction")
        return redacted
