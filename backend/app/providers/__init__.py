"""External data provider adapters and probes."""

from app.providers.amap_adapter import (
    AmapTravelDataProvider,
    load_fixture_amap_provider,
    open_live_amap_provider,
)
from app.providers.city_resolver import AmapCityResolverProvider, FixtureCityResolverProvider
from app.providers.errors import ProviderRequestError
from app.providers.ports import (
    CityResolverProvider,
    POISearchProvider,
    POISearchRequest,
    RetryPolicy,
    RouteProvider,
    RouteRequest,
    SpecialistProvider,
    StaySearchProvider,
    StaySearchRequest,
    TravelDataProvider,
    WeatherRiskProvider,
    WeatherRiskRequest,
)

__all__ = [
    "AmapCityResolverProvider",
    "AmapTravelDataProvider",
    "CityResolverProvider",
    "FixtureCityResolverProvider",
    "POISearchProvider",
    "POISearchRequest",
    "ProviderRequestError",
    "RetryPolicy",
    "RouteProvider",
    "RouteRequest",
    "SpecialistProvider",
    "StaySearchProvider",
    "StaySearchRequest",
    "TravelDataProvider",
    "WeatherRiskProvider",
    "WeatherRiskRequest",
    "load_fixture_amap_provider",
    "open_live_amap_provider",
]
