import hashlib
from collections import Counter
from datetime import date

from app.domain.candidates import ActivityEnvironment, CandidatePOI
from app.domain.planning import ActivityKind, TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.travel_data import RiskSeverity
from app.itinerary_quality import is_meal_candidate
from app.planning.material_contracts import PlanningMaterialBundle
from app.planning.weather_indoor_recovery_contracts import (
    WeatherIndoorCandidateObservation,
    WeatherIndoorRecoveryResult,
    WeatherIndoorRecoveryStatus,
    WeatherIndoorSearchQuery,
)
from app.planning.weather_repair import detect_weather_impacts
from app.providers.errors import ProviderRequestError
from app.providers.ports import POISearchProvider, POISearchRequest

SIGNIFICANT_WEATHER_SEVERITIES = frozenset(
    {RiskSeverity.MEDIUM, RiskSeverity.HIGH, RiskSeverity.EXTREME}
)
MAX_RECOVERY_QUERIES = 2
MAX_RESULTS_PER_QUERY = 5


def _stable_id(*values: object) -> str:
    material = "|".join(str(value) for value in values)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _explore_observations(materials: PlanningMaterialBundle) -> tuple[CandidatePOI, ...]:
    explore_branch = next(
        (
            item
            for item in materials.specialist_result.branches
            if item.specialist.value == "explore"
        ),
        None,
    )
    if explore_branch is None or explore_branch.explore_result is None:
        return ()
    return tuple(item.candidate for item in explore_branch.explore_result.observations)


def _eligible_indoor(candidate: CandidatePOI, *, city: str) -> bool:
    return (
        candidate.city == city
        and candidate.environment == ActivityEnvironment.INDOOR
        and not is_meal_candidate(candidate)
    )


def _affected_scope(
    plan: TripPlan,
    materials: PlanningMaterialBundle,
) -> tuple[tuple[date, ...], tuple[str, ...], tuple[str, ...], int]:
    significant_risks = tuple(
        item for item in plan.weather_risks if item.severity in SIGNIFICANT_WEATHER_SEVERITIES
    )
    if not significant_risks:
        return (), (), (), 0
    impacts = detect_weather_impacts(plan, materials, significant_risks)
    attraction_items = {
        item.item_id: item
        for day in plan.days
        for item in day.items
        if item.kind == ActivityKind.ATTRACTION
    }
    impacts = tuple(item for item in impacts if item.item_id in attraction_items)
    affected_dates = tuple(sorted({item.service_date for item in impacts}))
    affected_item_ids = tuple(dict.fromkeys(item.item_id for item in impacts))
    affected_candidate_ids = {item.candidate_id for item in impacts}
    candidate_by_id = {item.candidate_id: item for item in materials.shortlist.poi_candidates}
    district_counts: Counter[str] = Counter()
    for candidate_id in affected_candidate_ids:
        candidate = candidate_by_id.get(candidate_id)
        if candidate is not None and candidate.district:
            district_counts[candidate.district] += 1
    districts = tuple(district for district, _ in district_counts.most_common(2))
    return affected_dates, affected_item_ids, districts, len(affected_item_ids)


def _query_specs(districts: tuple[str, ...]) -> tuple[tuple[str, str | None], ...]:
    primary_district = districts[0] if districts else None
    return (
        ("博物馆", primary_district),
        ("科技馆", None),
    )


async def recover_weather_indoor_candidates(
    request: TripRequest,
    plan: TripPlan,
    materials: PlanningMaterialBundle,
    provider: POISearchProvider,
    *,
    data_mode: DataMode,
) -> WeatherIndoorRecoveryResult:
    """Close a weather-day indoor deficit with at most two grounded POI searches."""

    affected_dates, affected_item_ids, districts, required_count = _affected_scope(plan, materials)
    if required_count == 0:
        return WeatherIndoorRecoveryResult(
            request_id=request.request_id,
            data_mode=data_mode,
            status=WeatherIndoorRecoveryStatus.NOT_REQUIRED,
        )

    scheduled_ids = {
        item.candidate_id
        for day in plan.days
        for item in day.items
        if item.candidate_id is not None
    }
    existing_candidates = _explore_observations(materials)
    initial_reserve = tuple(
        item
        for item in existing_candidates
        if item.candidate_id not in scheduled_ids
        and _eligible_indoor(item, city=plan.destination_city)
    )
    initial_ids = {item.candidate_id for item in existing_candidates}
    if len(initial_reserve) >= required_count:
        return WeatherIndoorRecoveryResult(
            request_id=request.request_id,
            data_mode=data_mode,
            status=WeatherIndoorRecoveryStatus.SUFFICIENT,
            affected_dates=affected_dates,
            affected_item_ids=affected_item_ids,
            required_count=required_count,
            initial_available_count=len(initial_reserve),
        )

    queries: list[WeatherIndoorSearchQuery] = []
    observations: list[WeatherIndoorCandidateObservation] = []
    observed_ids = set(initial_ids)
    for keywords, district in _query_specs(districts)[:MAX_RECOVERY_QUERIES]:
        query = WeatherIndoorSearchQuery(
            query_id=f"weather-indoor-query-{_stable_id(request.request_id, keywords)}",
            keywords=keywords,
            reason=(
                "补充室内文化场馆并在生成方案时优先匹配受影响活动所在区域。"
                if district
                else "在目的城市扩大室内活动候选范围。"
            ),
            target_district=district,
        )
        queries.append(query)
        try:
            candidates = await provider.search_pois(
                POISearchRequest(
                    keywords=query.keywords,
                    city_adcode=request.destination_adcode or "000000",
                    limit=MAX_RESULTS_PER_QUERY,
                )
            )
        except ProviderRequestError:
            candidates = ()
        for candidate in candidates:
            if (
                candidate.candidate_id in observed_ids
                or not _eligible_indoor(candidate, city=plan.destination_city)
                or candidate.source.data_mode != data_mode
            ):
                continue
            observed_ids.add(candidate.candidate_id)
            observations.append(
                WeatherIndoorCandidateObservation(candidate=candidate, query_id=query.query_id)
            )
        if len(initial_reserve) + len(observations) >= required_count:
            break

    status = (
        WeatherIndoorRecoveryStatus.RECOVERED
        if len(initial_reserve) + len(observations) >= required_count
        else WeatherIndoorRecoveryStatus.INSUFFICIENT
    )
    return WeatherIndoorRecoveryResult(
        request_id=request.request_id,
        data_mode=data_mode,
        status=status,
        affected_dates=affected_dates,
        affected_item_ids=affected_item_ids,
        required_count=required_count,
        initial_available_count=len(initial_reserve),
        queries=tuple(queries),
        observations=tuple(observations),
        provider_call_count=len(queries),
    )
