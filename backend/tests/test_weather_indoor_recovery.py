import asyncio
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from app.domain.candidates import ActivityEnvironment, CandidatePOI, GeoPoint
from app.domain.planning import ActivityKind, DayPlan, ItineraryItem, PlanStatus, TripPlan
from app.domain.request import Party, TripPace, TripRequest
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import RiskSeverity, WeatherRisk, WeatherRiskType
from app.planning.material_contracts import PlanningMaterialBundle
from app.planning.specialist_contracts import SpecialistName
from app.planning.weather_indoor_recovery import recover_weather_indoor_candidates
from app.planning.weather_indoor_recovery_contracts import WeatherIndoorRecoveryStatus
from app.planning.weather_repair_contracts import WeatherImpact
from app.providers.ports import POISearchRequest

START_DATE = date(2026, 8, 31)


def _source(provider_id: str) -> SourceReference:
    return SourceReference(
        provider="weather-recovery-test",
        provider_id=provider_id,
        data_mode=DataMode.FIXTURE,
        retrieved_at=datetime(2026, 8, 26, tzinfo=UTC),
        raw_response_sha256="a" * 64,
    )


def _candidate(
    candidate_id: str,
    *,
    environment: ActivityEnvironment,
    district: str = "东城区",
) -> CandidatePOI:
    return CandidatePOI(
        candidate_id=candidate_id,
        name=candidate_id,
        city="北京市",
        district=district,
        address=f"{candidate_id}地址",
        location=GeoPoint(latitude=39.9, longitude=116.4 + len(candidate_id) / 1000),
        categories=("景点",),
        environment=environment,
        suggested_duration_minutes=120,
        tags=("测试",),
        source=_source(candidate_id),
    )


def _item(candidate: CandidatePOI, *, day: date, hour: int) -> ItineraryItem:
    start_at = datetime.combine(day, time(hour), tzinfo=UTC)
    return ItineraryItem(
        item_id=f"item-{day.isoformat()}-{candidate.candidate_id}",
        kind=ActivityKind.ATTRACTION,
        title=candidate.name,
        start_at=start_at,
        end_at=start_at + timedelta(hours=2),
        candidate_id=candidate.candidate_id,
        source=candidate.source,
    )


def _request() -> TripRequest:
    return TripRequest(
        request_id="weather-recovery-request",
        raw_text="北京两日游",
        destination_city="北京市",
        destination_adcode="110000",
        start_date=START_DATE,
        end_date=START_DATE + timedelta(days=1),
        party=Party(adults=2),
        pace=TripPace.RELAXED,
    )


def _plan(
    outdoors: tuple[CandidatePOI, ...],
    *,
    severity: RiskSeverity = RiskSeverity.MEDIUM,
) -> TripPlan:
    risk = WeatherRisk(
        risk_id="rain-risk",
        city="北京市",
        starts_at=datetime.combine(START_DATE, time.min, tzinfo=UTC),
        ends_at=datetime.combine(START_DATE + timedelta(days=1), time.min, tzinfo=UTC),
        risk_type=WeatherRiskType.RAIN,
        severity=severity,
        threshold_description="中雨",
        affected_activity_types=("outdoor",),
        advisory="优先室内活动",
        source=_source("rain"),
    )
    return TripPlan(
        plan_id="weather-recovery-plan",
        request_id="weather-recovery-request",
        status=PlanStatus.DRAFT,
        destination_city="北京市",
        start_date=START_DATE,
        end_date=START_DATE + timedelta(days=1),
        days=(
            DayPlan(
                date=START_DATE,
                items=tuple(
                    _item(candidate, day=START_DATE, hour=9 + index * 3)
                    for index, candidate in enumerate(outdoors)
                ),
                weather_risk_ids=(risk.risk_id,),
            ),
            DayPlan(
                date=START_DATE + timedelta(days=1),
                items=(_item(outdoors[0], day=START_DATE + timedelta(days=1), hour=9),),
            ),
        ),
        weather_risks=(risk,),
    )


def _materials(
    scheduled: tuple[CandidatePOI, ...],
    reserve: tuple[CandidatePOI, ...] = (),
) -> PlanningMaterialBundle:
    observations = tuple(SimpleNamespace(candidate=item) for item in (*scheduled, *reserve))
    branch = SimpleNamespace(
        specialist=SpecialistName.EXPLORE,
        explore_result=SimpleNamespace(observations=observations),
    )
    return cast(
        PlanningMaterialBundle,
        SimpleNamespace(
            specialist_result=SimpleNamespace(branches=(branch,)),
            shortlist=SimpleNamespace(poi_candidates=scheduled),
        ),
    )


def _impacts(outdoors: tuple[CandidatePOI, ...]) -> tuple[WeatherImpact, ...]:
    return tuple(
        WeatherImpact(
            risk_id="rain-risk",
            item_id=f"item-{START_DATE.isoformat()}-{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
            service_date=START_DATE,
            environment=ActivityEnvironment.OUTDOOR,
            severity=RiskSeverity.MEDIUM,
            matched_activity_types=("outdoor",),
            risk_source=_source("rain"),
        )
        for candidate in outdoors
    )


class _IndoorProvider:
    def __init__(self, pages: tuple[tuple[CandidatePOI, ...], ...]) -> None:
        self.pages = pages
        self.requests: list[POISearchRequest] = []

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        self.requests.append(request)
        return self.pages[len(self.requests) - 1]


def test_low_weather_notice_does_not_trigger_provider_recovery() -> None:
    outdoors = tuple(
        _candidate(f"outdoor-{index}", environment=ActivityEnvironment.OUTDOOR)
        for index in range(2)
    )
    provider = _IndoorProvider(())

    result = asyncio.run(
        recover_weather_indoor_candidates(
            _request(),
            _plan(outdoors, severity=RiskSeverity.LOW),
            _materials(outdoors),
            provider,
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == WeatherIndoorRecoveryStatus.NOT_REQUIRED
    assert result.provider_call_count == 0
    assert provider.requests == []


def test_recovery_skips_provider_when_existing_indoor_reserve_is_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outdoors = tuple(
        _candidate(f"outdoor-{index}", environment=ActivityEnvironment.OUTDOOR)
        for index in range(2)
    )
    reserve = tuple(
        _candidate(f"indoor-{index}", environment=ActivityEnvironment.INDOOR) for index in range(2)
    )
    monkeypatch.setattr(
        "app.planning.weather_indoor_recovery.detect_weather_impacts",
        lambda *_args: _impacts(outdoors),
    )
    provider = _IndoorProvider(())

    result = asyncio.run(
        recover_weather_indoor_candidates(
            _request(),
            _plan(outdoors),
            _materials(outdoors, reserve),
            provider,
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == WeatherIndoorRecoveryStatus.SUFFICIENT
    assert result.required_count == 2
    assert result.initial_available_count == 2
    assert result.provider_call_count == 0
    assert provider.requests == []


def test_recovery_prepares_distinct_candidates_for_all_affected_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outdoors = tuple(
        _candidate(f"outdoor-{index}", environment=ActivityEnvironment.OUTDOOR)
        for index in range(2)
    )
    reserve = tuple(
        _candidate(f"indoor-{index}", environment=ActivityEnvironment.INDOOR) for index in range(2)
    )
    second_day_impact = WeatherImpact(
        risk_id="rain-risk",
        item_id=f"item-{(START_DATE + timedelta(days=1)).isoformat()}-{outdoors[0].candidate_id}",
        candidate_id=outdoors[0].candidate_id,
        service_date=START_DATE + timedelta(days=1),
        environment=ActivityEnvironment.OUTDOOR,
        severity=RiskSeverity.MEDIUM,
        matched_activity_types=("outdoor",),
        risk_source=_source("rain"),
    )
    monkeypatch.setattr(
        "app.planning.weather_indoor_recovery.detect_weather_impacts",
        lambda *_args: (*_impacts(outdoors), second_day_impact),
    )
    provider = _IndoorProvider(
        ((_candidate("indoor-third", environment=ActivityEnvironment.INDOOR),),)
    )

    result = asyncio.run(
        recover_weather_indoor_candidates(
            _request(),
            _plan(outdoors),
            _materials(outdoors, reserve),
            provider,
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == WeatherIndoorRecoveryStatus.RECOVERED
    assert result.affected_dates == (START_DATE, START_DATE + timedelta(days=1))
    assert result.required_count == 3
    assert result.available_count == 3
    assert result.provider_call_count == 1


def test_recovery_searches_until_the_whole_weather_day_is_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outdoors = tuple(
        _candidate(f"outdoor-{index}", environment=ActivityEnvironment.OUTDOOR)
        for index in range(3)
    )
    first_page = (
        _candidate("indoor-one", environment=ActivityEnvironment.INDOOR),
        _candidate("unknown-one", environment=ActivityEnvironment.UNKNOWN),
    )
    second_page = (
        _candidate("indoor-two", environment=ActivityEnvironment.INDOOR),
        _candidate("indoor-three", environment=ActivityEnvironment.INDOOR),
    )
    monkeypatch.setattr(
        "app.planning.weather_indoor_recovery.detect_weather_impacts",
        lambda *_args: _impacts(outdoors),
    )
    provider = _IndoorProvider((first_page, second_page))

    result = asyncio.run(
        recover_weather_indoor_candidates(
            _request(),
            _plan(outdoors),
            _materials(outdoors),
            provider,
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == WeatherIndoorRecoveryStatus.RECOVERED
    assert result.required_count == 3
    assert result.available_count == 3
    assert result.provider_call_count == 2
    assert [item.candidate.candidate_id for item in result.observations] == [
        "indoor-one",
        "indoor-two",
        "indoor-three",
    ]
    assert result.queries[0].target_district == "东城区"
    assert [item.keywords for item in provider.requests] == ["博物馆", "科技馆"]


def test_recovery_preserves_the_plan_when_grounded_candidates_remain_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outdoors = tuple(
        _candidate(f"outdoor-{index}", environment=ActivityEnvironment.OUTDOOR)
        for index in range(3)
    )
    monkeypatch.setattr(
        "app.planning.weather_indoor_recovery.detect_weather_impacts",
        lambda *_args: _impacts(outdoors),
    )
    provider = _IndoorProvider(
        (
            (_candidate("indoor-one", environment=ActivityEnvironment.INDOOR),),
            (_candidate("outdoor-extra", environment=ActivityEnvironment.OUTDOOR),),
        )
    )

    plan = _plan(outdoors)
    result = asyncio.run(
        recover_weather_indoor_candidates(
            _request(),
            plan,
            _materials(outdoors),
            provider,
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == WeatherIndoorRecoveryStatus.INSUFFICIENT
    assert result.available_count == 1
    assert result.provider_call_count == 2
    assert plan.plan_id == "weather-recovery-plan"
