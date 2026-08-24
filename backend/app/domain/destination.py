from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.sources import DataMode, SourceReference


class AdministrativeLevel(StrEnum):
    PROVINCE = "province"
    CITY = "city"
    DISTRICT = "district"


class DestinationResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NO_RESULT = "no_result"
    UNSUPPORTED = "unsupported"


class CityResolutionCandidate(DomainModel):
    candidate_id: Identifier
    qualified_name: NonEmptyText
    planning_city_name: NonEmptyText
    administrative_code: str = Field(pattern=r"^\d{6}$")
    level: AdministrativeLevel
    province_name: NonEmptyText | None = None
    city_name: NonEmptyText | None = None
    district_name: NonEmptyText | None = None
    center: str | None = Field(default=None, pattern=r"^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$")
    source: SourceReference


class DestinationResolution(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    resolver_version: Literal["city-resolver-v1"] = "city-resolver-v1"
    input_name: NonEmptyText
    data_mode: Literal[DataMode.FIXTURE, DataMode.LIVE]
    status: DestinationResolutionStatus
    candidates: tuple[CityResolutionCandidate, ...] = ()

    @model_validator(mode="after")
    def validate_resolution_shape(self) -> "DestinationResolution":
        codes = [candidate.administrative_code for candidate in self.candidates]
        if len(codes) != len(set(codes)):
            raise ValueError("city resolution candidates must have unique administrative codes")
        if self.status == DestinationResolutionStatus.RESOLVED and len(self.candidates) != 1:
            raise ValueError("resolved destination must contain exactly one candidate")
        if self.status == DestinationResolutionStatus.AMBIGUOUS and len(self.candidates) < 2:
            raise ValueError("ambiguous destination must contain at least two candidates")
        if (
            self.status
            in {
                DestinationResolutionStatus.NO_RESULT,
                DestinationResolutionStatus.UNSUPPORTED,
            }
            and self.candidates
        ):
            raise ValueError("terminal destination resolution cannot contain candidates")
        return self

    def select(self, administrative_code: str | None = None) -> CityResolutionCandidate:
        if administrative_code is not None:
            for candidate in self.candidates:
                if candidate.administrative_code == administrative_code:
                    return candidate
            raise ValueError("selected destination is not present in the resolver result")
        if self.status != DestinationResolutionStatus.RESOLVED:
            raise ValueError("destination requires an explicit candidate selection")
        return self.candidates[0]
