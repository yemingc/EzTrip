from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.candidates import GeoPoint
from app.domain.sources import DataMode, SourceReference


class WeatherRiskType(StrEnum):
    RAIN = "rain"
    HEAT = "heat"
    WIND = "wind"
    SNOW = "snow"
    COLD = "cold"
    AIR_QUALITY = "air_quality"


class RiskSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


class RouteMode(StrEnum):
    WALKING = "walking"
    TRANSIT = "transit"
    DRIVING = "driving"
    CYCLING = "cycling"


class RouteEndpoint(DomainModel):
    name: NonEmptyText
    candidate_id: Identifier | None = None
    location: GeoPoint


class WeatherRisk(DomainModel):
    risk_id: Identifier
    city: NonEmptyText
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    risk_type: WeatherRiskType
    severity: RiskSeverity
    metrics: dict[NonEmptyText, float] = Field(default_factory=dict)
    threshold_description: NonEmptyText
    affected_activity_types: tuple[NonEmptyText, ...] = Field(min_length=1)
    advisory: NonEmptyText
    source: SourceReference

    @model_validator(mode="after")
    def validate_weather_origin(self) -> "WeatherRisk":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.source.data_mode not in {DataMode.LIVE, DataMode.FIXTURE}:
            raise ValueError("WeatherRisk must originate from live or fixture weather tool data")
        return self


class RouteLeg(DomainModel):
    route_leg_id: Identifier
    origin: RouteEndpoint
    destination: RouteEndpoint
    mode: RouteMode
    distance_meters: int = Field(ge=0)
    duration_minutes: int = Field(gt=0, le=1440)
    source: SourceReference

    @model_validator(mode="after")
    def validate_route_origin(self) -> "RouteLeg":
        if self.source.data_mode not in {DataMode.LIVE, DataMode.FIXTURE}:
            raise ValueError("RouteLeg must originate from live or fixture route data")
        return self
