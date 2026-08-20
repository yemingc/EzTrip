from enum import StrEnum

from pydantic import Field, model_validator

from app.domain.base import DomainModel, NonEmptyText


class ProviderErrorCategory(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    EMPTY_RESULT = "empty_result"
    MISSING_FIELD = "missing_field"
    AUTHENTICATION_FAILED = "authentication_failed"
    UNRECOVERABLE = "unrecoverable"


class ProviderFailure(DomainModel):
    provider: NonEmptyText
    operation: NonEmptyText
    category: ProviderErrorCategory
    message: NonEmptyText
    retryable: bool
    retry_after_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_retry_contract(self) -> "ProviderFailure":
        terminal_categories = {
            ProviderErrorCategory.AUTHENTICATION_FAILED,
            ProviderErrorCategory.UNRECOVERABLE,
        }
        if self.category in terminal_categories and self.retryable:
            raise ValueError("authentication and unrecoverable failures cannot be retryable")
        if (
            self.retry_after_seconds is not None
            and self.category != ProviderErrorCategory.RATE_LIMITED
        ):
            raise ValueError("retry_after_seconds is only valid for rate-limited failures")
        return self
