from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.candidates import ActivityEnvironment, CandidatePOI
from app.domain.sources import DataMode
from app.itinerary_quality import is_meal_candidate


class WeatherIndoorRecoveryStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    SUFFICIENT = "sufficient"
    RECOVERED = "recovered"
    INSUFFICIENT = "insufficient"


class WeatherIndoorSearchQuery(DomainModel):
    query_id: Identifier
    keywords: str = Field(min_length=1, max_length=40)
    reason: NonEmptyText
    target_district: NonEmptyText | None = None


class WeatherIndoorCandidateObservation(DomainModel):
    candidate: CandidatePOI
    query_id: Identifier

    @model_validator(mode="after")
    def validate_indoor_candidate(self) -> "WeatherIndoorCandidateObservation":
        if self.candidate.environment != ActivityEnvironment.INDOOR:
            raise ValueError("weather recovery observations must be indoor candidates")
        if is_meal_candidate(self.candidate):
            raise ValueError("weather recovery observations cannot be dining candidates")
        return self


class WeatherIndoorRecoveryResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    recovery_version: Literal["weather-indoor-recovery-v1"] = "weather-indoor-recovery-v1"
    request_id: Identifier
    data_mode: Literal[DataMode.LIVE, DataMode.FIXTURE]
    status: WeatherIndoorRecoveryStatus
    affected_dates: tuple[date, ...] = Field(default=(), max_length=5)
    affected_item_ids: tuple[Identifier, ...] = Field(default=(), max_length=20)
    required_count: int = Field(default=0, ge=0, le=20)
    initial_available_count: int = Field(default=0, ge=0, le=25)
    queries: tuple[WeatherIndoorSearchQuery, ...] = Field(default=(), max_length=2)
    observations: tuple[WeatherIndoorCandidateObservation, ...] = Field(default=(), max_length=10)
    provider_call_count: int = Field(default=0, ge=0, le=2)

    @property
    def available_count(self) -> int:
        return self.initial_available_count + len(self.observations)

    @model_validator(mode="after")
    def validate_recovery(self) -> "WeatherIndoorRecoveryResult":
        for values in (self.affected_dates, self.affected_item_ids):
            if len(values) != len(set(values)):
                raise ValueError("weather recovery scopes must contain unique values")
        query_ids = tuple(item.query_id for item in self.queries)
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("weather recovery queries must be unique")
        candidate_ids = tuple(item.candidate.candidate_id for item in self.observations)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("weather recovery observations must be unique")
        if any(item.query_id not in query_ids for item in self.observations):
            raise ValueError("weather recovery observations must reference a recovery query")
        if any(item.candidate.source.data_mode != self.data_mode for item in self.observations):
            raise ValueError("weather recovery candidates must preserve Provider data mode")
        if self.provider_call_count != len(self.queries):
            raise ValueError("weather recovery call count must match attempted queries")

        if self.status == WeatherIndoorRecoveryStatus.NOT_REQUIRED:
            if any(
                (
                    self.affected_dates,
                    self.affected_item_ids,
                    self.required_count,
                    self.initial_available_count,
                    self.queries,
                    self.observations,
                    self.provider_call_count,
                )
            ):
                raise ValueError("not-required weather recovery cannot contain recovery work")
            return self

        if not self.affected_dates or not self.affected_item_ids or self.required_count < 1:
            raise ValueError("weather recovery requires an affected day and activity scope")
        if self.status == WeatherIndoorRecoveryStatus.SUFFICIENT:
            if self.initial_available_count < self.required_count or any(
                (self.queries, self.observations, self.provider_call_count)
            ):
                raise ValueError("sufficient recovery must reuse the existing indoor reserve")
            return self

        if not self.queries:
            raise ValueError("attempted weather recovery requires at least one query")
        if self.status == WeatherIndoorRecoveryStatus.RECOVERED:
            if self.available_count < self.required_count or not self.observations:
                raise ValueError("recovered weather candidates must close the indoor deficit")
        elif self.available_count >= self.required_count:
            raise ValueError("insufficient weather recovery must retain an indoor deficit")
        return self
