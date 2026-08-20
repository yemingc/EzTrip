from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.money import MoneyRange
from app.domain.sources import DataMode, SourceReference


class ActivityEnvironment(StrEnum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class StayPriceBasis(StrEnum):
    USER_INPUT = "user_input"
    FIXTURE_ESTIMATE = "fixture_estimate"
    HISTORICAL_ESTIMATE = "historical_estimate"


class GeoPoint(DomainModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class CandidatePOI(DomainModel):
    candidate_id: Identifier
    name: NonEmptyText
    city: NonEmptyText
    district: NonEmptyText | None = None
    address: NonEmptyText | None = None
    location: GeoPoint
    categories: tuple[NonEmptyText, ...] = Field(min_length=1)
    environment: ActivityEnvironment = ActivityEnvironment.UNKNOWN
    suggested_duration_minutes: int | None = Field(default=None, gt=0, le=720)
    tags: tuple[NonEmptyText, ...] = ()
    source: SourceReference

    @model_validator(mode="after")
    def validate_provider_identity(self) -> "CandidatePOI":
        if self.source.provider_id is None:
            raise ValueError("candidate POIs require a provider_id")
        if self.source.data_mode not in {DataMode.LIVE, DataMode.FIXTURE}:
            raise ValueError("candidate POIs must originate from live or fixture provider data")
        return self


class CandidateStay(DomainModel):
    candidate_id: Identifier
    name: NonEmptyText
    city: NonEmptyText
    district: NonEmptyText | None = None
    address: NonEmptyText | None = None
    location: GeoPoint
    area_name: NonEmptyText
    tags: tuple[NonEmptyText, ...] = ()
    nightly_price_estimate: MoneyRange | None = None
    price_basis: StayPriceBasis | None = None
    price_source: SourceReference | None = None
    availability_status: Literal["unknown"] = "unknown"
    booking_supported: Literal[False] = False
    source: SourceReference

    @model_validator(mode="after")
    def validate_truth_boundary(self) -> "CandidateStay":
        if self.source.provider_id is None:
            raise ValueError("stay candidates require a provider_id")
        if self.source.data_mode not in {DataMode.LIVE, DataMode.FIXTURE}:
            raise ValueError("stay candidates must originate from live or fixture provider data")
        estimate_fields = (
            self.nightly_price_estimate,
            self.price_basis,
            self.price_source,
        )
        if any(field is not None for field in estimate_fields) and not all(
            field is not None for field in estimate_fields
        ):
            raise ValueError("stay price estimate, basis, and source must be provided together")
        if self.price_source is not None:
            assert self.price_basis is not None
            expected_mode = {
                StayPriceBasis.USER_INPUT: DataMode.USER_INPUT,
                StayPriceBasis.FIXTURE_ESTIMATE: DataMode.FIXTURE,
                StayPriceBasis.HISTORICAL_ESTIMATE: DataMode.ESTIMATE,
            }[self.price_basis]
            if self.price_source.data_mode != expected_mode:
                raise ValueError("stay price basis must match the explicit price source data mode")
        return self
