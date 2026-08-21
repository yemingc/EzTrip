from datetime import date, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier
from app.domain.sources import DataMode, SourceReference


class OpeningHoursEvidence(DomainModel):
    evidence_id: Identifier
    candidate_id: Identifier
    service_date: date
    opens_at: AwareDatetime
    closes_at: AwareDatetime
    source: SourceReference

    @model_validator(mode="after")
    def validate_window(self) -> "OpeningHoursEvidence":
        if self.closes_at <= self.opens_at:
            raise ValueError("opening-hours evidence must close after it opens")
        if self.opens_at.date() != self.service_date:
            raise ValueError("service_date must match the opening timestamp date")
        if self.closes_at.date() not in {
            self.opens_at.date(),
            self.opens_at.date() + timedelta(days=1),
        }:
            raise ValueError("opening-hours evidence may span at most one overnight window")
        if self.source.data_mode not in {DataMode.LIVE, DataMode.FIXTURE}:
            raise ValueError("opening-hours evidence must originate from live or fixture data")
        if self.source.provider_id is None:
            raise ValueError("opening-hours evidence requires source.provider_id")
        return self


class OpeningHoursEvidenceBundle(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    evidence_version: Literal["opening-hours-evidence-v1"] = "opening-hours-evidence-v1"
    request_id: Identifier
    data_mode: Literal[DataMode.LIVE, DataMode.FIXTURE]
    items: tuple[OpeningHoursEvidence, ...] = Field(default=(), max_length=40)

    @model_validator(mode="after")
    def validate_bundle(self) -> "OpeningHoursEvidenceBundle":
        evidence_ids = [item.evidence_id for item in self.items]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("opening-hours evidence ids must be unique")
        windows = [
            (item.candidate_id, item.service_date, item.opens_at, item.closes_at)
            for item in self.items
        ]
        if len(windows) != len(set(windows)):
            raise ValueError("opening-hours evidence windows must be unique")
        if any(item.source.data_mode != self.data_mode for item in self.items):
            raise ValueError("opening-hours evidence must match the bundle data mode")
        return self
