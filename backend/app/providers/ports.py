from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import Field

from app.domain.base import DomainModel, NonEmptyText
from app.domain.candidates import CandidatePOI, CandidateStay
from app.domain.destination import DestinationResolution
from app.domain.travel_data import RouteEndpoint, RouteLeg, RouteMode, WeatherRisk


class POISearchRequest(DomainModel):
    keywords: NonEmptyText
    city_adcode: str = Field(pattern=r"^\d{6}$")
    limit: int = Field(default=1, ge=1, le=5)


class StaySearchRequest(DomainModel):
    keywords: NonEmptyText
    city_adcode: str = Field(pattern=r"^\d{6}$")
    limit: int = Field(default=1, ge=1, le=3)


class WeatherRiskRequest(DomainModel):
    city_adcode: str = Field(pattern=r"^\d{6}$")


class RouteRequest(DomainModel):
    origin: RouteEndpoint
    destination: RouteEndpoint
    mode: RouteMode
    city_adcode: str = Field(pattern=r"^\d{6}$")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    base_delay_seconds: float = 0.05
    max_delay_seconds: float = 0.5

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds cannot be below base_delay_seconds")

    def delay_for_retry(self, retry_number: int) -> float:
        exponent = max(retry_number - 1, 0)
        delay = self.base_delay_seconds * (2.0**exponent)
        return float(min(delay, self.max_delay_seconds))


Sleep = Callable[[float], Awaitable[None]]


class POISearchProvider(Protocol):
    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]: ...


class StaySearchProvider(Protocol):
    async def search_stays(self, request: StaySearchRequest) -> tuple[CandidateStay, ...]: ...


class WeatherRiskProvider(Protocol):
    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]: ...


class RouteProvider(Protocol):
    async def get_route(self, request: RouteRequest) -> RouteLeg: ...


class CityResolverProvider(Protocol):
    async def resolve_destination(self, input_name: str) -> DestinationResolution: ...


class SpecialistProvider(
    POISearchProvider,
    StaySearchProvider,
    WeatherRiskProvider,
    Protocol,
): ...


class TravelDataProvider(POISearchProvider, WeatherRiskProvider, RouteProvider, Protocol): ...
