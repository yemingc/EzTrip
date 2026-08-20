from enum import StrEnum

from pydantic import AwareDatetime

from app.domain.base import DomainModel, NonEmptyText, Sha256Digest


class DataMode(StrEnum):
    LIVE = "live"
    FIXTURE = "fixture"
    USER_INPUT = "user_input"
    ESTIMATE = "estimate"


class SourceReference(DomainModel):
    """Traceable origin for provider data or an explicit estimate."""

    provider: NonEmptyText
    provider_id: NonEmptyText | None = None
    data_mode: DataMode
    retrieved_at: AwareDatetime
    raw_response_sha256: Sha256Digest | None = None
