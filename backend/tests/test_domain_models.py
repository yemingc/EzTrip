from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain import (
    ActivityKind,
    BudgetCategory,
    BudgetConstraint,
    CandidatePOI,
    CandidateStay,
    Constraint,
    ConstraintKind,
    ConstraintSet,
    ConstraintSource,
    ConstraintStrength,
    CostItem,
    DataMode,
    DayPlan,
    GeoPoint,
    IssueSeverity,
    ItineraryItem,
    MoneyRange,
    Party,
    PlanStatus,
    PlanVersion,
    ProviderErrorCategory,
    ProviderFailure,
    RepairAction,
    ResponsibleNode,
    RiskSeverity,
    RouteEndpoint,
    RouteLeg,
    RouteMode,
    SourceReference,
    StayPriceBasis,
    TripPlan,
    TripRequest,
    ValidationEvidence,
    ValidationIssue,
    WeatherRisk,
    WeatherRiskType,
)

CHINA_TZ = timezone(timedelta(hours=8))


def source_reference(
    *,
    provider_id: str | None = "provider_item_001",
    data_mode: DataMode = DataMode.FIXTURE,
) -> SourceReference:
    return SourceReference(
        provider="test_provider",
        provider_id=provider_id,
        data_mode=data_mode,
        retrieved_at=datetime(2026, 9, 20, 8, tzinfo=CHINA_TZ),
    )


def itinerary_item(
    item_id: str,
    day: date,
    start_hour: int,
    end_hour: int,
    *,
    cost_item_ids: tuple[str, ...] = (),
) -> ItineraryItem:
    return ItineraryItem(
        item_id=item_id,
        kind=ActivityKind.ATTRACTION,
        title="测试景点",
        start_at=datetime.combine(day, datetime.min.time(), CHINA_TZ).replace(hour=start_hour),
        end_at=datetime.combine(day, datetime.min.time(), CHINA_TZ).replace(hour=end_hour),
        candidate_id=f"poi_{item_id}",
        source=source_reference(provider_id=f"provider_{item_id}"),
        cost_item_ids=cost_item_ids,
    )


def three_day_plan(*, referenced_cost_id: str | None = None) -> TripPlan:
    start = date(2026, 10, 2)
    cost_item_ids = (referenced_cost_id,) if referenced_cost_id else ()
    days = tuple(
        DayPlan(
            date=start + timedelta(days=offset),
            items=(
                itinerary_item(
                    f"item_day_{offset + 1}",
                    start + timedelta(days=offset),
                    9,
                    11,
                    cost_item_ids=cost_item_ids if offset == 0 else (),
                ),
            ),
        )
        for offset in range(3)
    )
    return TripPlan(
        plan_id="plan_beijing_test",
        request_id="request_beijing_test",
        status=PlanStatus.DRAFT,
        destination_city="北京市",
        start_date=start,
        end_date=start + timedelta(days=2),
        days=days,
    )


@pytest.mark.parametrize(
    "party",
    [
        {"adults": 0, "children": 0, "seniors": 0},
        {"adults": 0, "children": 1, "seniors": 0},
        {"adults": 1, "children": 0, "seniors": 0, "rooms": 2},
    ],
)
def test_party_rejects_invalid_compositions(party: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Party.model_validate(party)


def test_budget_rejects_duplicate_categories() -> None:
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        BudgetConstraint(
            total_limit="1000",
            included_categories=(BudgetCategory.FOOD, BudgetCategory.FOOD),
        )


def test_constraint_set_rejects_duplicate_ids_and_hard_conflicts() -> None:
    base: dict[str, Any] = {
        "constraint_id": "place_rule",
        "value": "故宫",
        "strength": ConstraintStrength.HARD,
        "priority": 5,
        "source": ConstraintSource.USER_EXPLICIT,
        "confirmed": True,
    }
    must_visit = Constraint(kind=ConstraintKind.MUST_VISIT, **base)

    with pytest.raises(ValidationError, match="constraint_id values must be unique"):
        ConstraintSet(items=(must_visit, must_visit))

    avoid = Constraint(
        kind=ConstraintKind.AVOID,
        **{**base, "constraint_id": "avoid_place"},
    )
    with pytest.raises(ValidationError, match="must_visit and avoid"):
        ConstraintSet(items=(must_visit, avoid))


@pytest.mark.parametrize(
    ("start", "end"),
    [(date(2026, 10, 3), date(2026, 10, 2)), (date(2026, 10, 2), date(2026, 10, 2))],
)
def test_trip_request_rejects_invalid_v1_date_windows(start: date, end: date) -> None:
    with pytest.raises(ValidationError):
        TripRequest(
            request_id="request_invalid_window",
            raw_text="规划一个不符合 V1 天数范围的测试行程",
            destination_city="北京市",
            start_date=start,
            end_date=end,
            party=Party(adults=1),
        )


def test_trip_request_rejects_constraint_dates_outside_the_trip() -> None:
    constraint = Constraint(
        constraint_id="scheduled_museum",
        kind=ConstraintKind.SCHEDULE,
        value="博物馆安排在指定日期",
        strength=ConstraintStrength.HARD,
        priority=5,
        source=ConstraintSource.USER_EXPLICIT,
        applies_to_dates=(date(2026, 10, 8),),
        confirmed=True,
    )
    with pytest.raises(ValidationError, match="constraint dates"):
        TripRequest(
            request_id="request_invalid_constraint_date",
            raw_text="规划北京三日游并把博物馆安排在行程之外的日期",
            destination_city="北京市",
            start_date=date(2026, 10, 2),
            end_date=date(2026, 10, 4),
            party=Party(adults=1),
            constraints=ConstraintSet(items=(constraint,)),
        )


def test_money_range_and_cost_item_keep_estimates_explicit() -> None:
    with pytest.raises(ValidationError, match="maximum must be"):
        MoneyRange(minimum="20", maximum="10")

    with pytest.raises(ValidationError, match="is_estimate=true"):
        CostItem(
            cost_item_id="cost_hotel_estimate",
            category=BudgetCategory.LODGING,
            description="住宿估算",
            quantity="2",
            unit_price=MoneyRange(minimum="300", maximum="500"),
            source=source_reference(data_mode=DataMode.ESTIMATE),
            is_estimate=False,
        )

    exact = CostItem(
        cost_item_id="cost_ticket_exact",
        category=BudgetCategory.ADMISSION,
        description="门票 fixture",
        quantity="2",
        unit_price=MoneyRange(minimum="60", maximum="60"),
        source=source_reference(),
        is_estimate=False,
    )
    assert exact.total_minimum == Decimal("120")
    assert exact.total_maximum == Decimal("120")


def test_candidates_require_traceable_provider_ids_and_complete_stay_estimates() -> None:
    point = GeoPoint(latitude=39.9163, longitude=116.3972)
    with pytest.raises(ValidationError, match="provider_id"):
        CandidatePOI(
            candidate_id="poi_untraced",
            name="未追溯景点",
            city="北京市",
            location=point,
            categories=("景点",),
            source=source_reference(provider_id=None),
        )

    with pytest.raises(ValidationError, match="must be provided together"):
        CandidateStay(
            candidate_id="stay_incomplete_price",
            name="测试酒店",
            city="北京市",
            location=point,
            area_name="王府井",
            nightly_price_estimate=MoneyRange(minimum="300", maximum="500"),
            source=source_reference(),
        )

    stay = CandidateStay(
        candidate_id="stay_honest_estimate",
        name="测试酒店",
        city="北京市",
        location=point,
        area_name="王府井",
        nightly_price_estimate=MoneyRange(minimum="300", maximum="500"),
        price_basis=StayPriceBasis.FIXTURE_ESTIMATE,
        price_source=source_reference(data_mode=DataMode.FIXTURE),
        source=source_reference(),
    )
    assert stay.availability_status == "unknown"
    assert stay.booking_supported is False

    with pytest.raises(ValidationError, match="price basis must match"):
        CandidateStay.model_validate(
            {
                **stay.model_dump(),
                "price_source": source_reference(data_mode=DataMode.ESTIMATE),
            }
        )


def test_weather_risk_must_come_from_a_weather_tool_and_have_a_valid_window() -> None:
    payload: dict[str, Any] = {
        "risk_id": "weather_rain_test",
        "city": "北京市",
        "starts_at": datetime(2026, 10, 3, 8, tzinfo=CHINA_TZ),
        "ends_at": datetime(2026, 10, 3, 18, tzinfo=CHINA_TZ),
        "risk_type": WeatherRiskType.RAIN,
        "severity": RiskSeverity.HIGH,
        "metrics": {"precipitation_probability": 0.9},
        "threshold_description": "降雨概率大于等于 0.8",
        "affected_activity_types": ("outdoor", "photography"),
        "advisory": "建议调整室外活动",
        "source": source_reference(),
    }
    risk = WeatherRisk.model_validate(payload)
    assert risk.source.data_mode == DataMode.FIXTURE

    with pytest.raises(ValidationError, match="weather tool data"):
        WeatherRisk.model_validate(
            {**payload, "source": source_reference(data_mode=DataMode.USER_INPUT)}
        )
    with pytest.raises(ValidationError, match="ends_at must be after"):
        WeatherRisk.model_validate({**payload, "ends_at": payload["starts_at"]})


def test_route_leg_must_come_from_route_data() -> None:
    point = GeoPoint(latitude=39.9, longitude=116.4)
    payload = {
        "route_leg_id": "route_test",
        "origin": RouteEndpoint(name="起点", location=point),
        "destination": RouteEndpoint(name="终点", location=point),
        "mode": RouteMode.TRANSIT,
        "distance_meters": 1000,
        "duration_minutes": 20,
        "source": source_reference(data_mode=DataMode.USER_INPUT),
    }
    with pytest.raises(ValidationError, match="route data"):
        RouteLeg.model_validate(payload)


def test_trip_plan_rejects_weather_risks_for_another_city_or_date_range() -> None:
    plan = three_day_plan()
    risk = WeatherRisk(
        risk_id="weather_plan_test",
        city="北京市",
        starts_at=datetime(2026, 10, 3, 8, tzinfo=CHINA_TZ),
        ends_at=datetime(2026, 10, 3, 18, tzinfo=CHINA_TZ),
        risk_type=WeatherRiskType.RAIN,
        severity=RiskSeverity.HIGH,
        threshold_description="降雨概率大于等于 0.8",
        affected_activity_types=("outdoor",),
        advisory="调整室外活动",
        source=source_reference(),
    )
    days = (
        plan.days[0],
        plan.days[1].model_copy(update={"weather_risk_ids": (risk.risk_id,)}),
        plan.days[2],
    )

    with pytest.raises(ValidationError, match="destination city"):
        TripPlan.model_validate(
            {
                **plan.model_dump(),
                "days": days,
                "weather_risks": (risk.model_copy(update={"city": "上海市"}),),
            }
        )

    future_risk = risk.model_copy(
        update={
            "starts_at": datetime(2026, 10, 8, 8, tzinfo=CHINA_TZ),
            "ends_at": datetime(2026, 10, 8, 18, tzinfo=CHINA_TZ),
        }
    )
    with pytest.raises(ValidationError, match="overlap the trip date range"):
        TripPlan.model_validate(
            {**plan.model_dump(), "days": days, "weather_risks": (future_risk,)}
        )


def test_itinerary_and_day_plan_reject_ungrounded_or_overlapping_items() -> None:
    trip_date = date(2026, 10, 2)
    with pytest.raises(ValidationError, match="require candidate_id and source"):
        ItineraryItem(
            item_id="item_ungrounded",
            kind=ActivityKind.ATTRACTION,
            title="无来源景点",
            start_at=datetime(2026, 10, 2, 9, tzinfo=CHINA_TZ),
            end_at=datetime(2026, 10, 2, 10, tzinfo=CHINA_TZ),
        )

    first = itinerary_item("item_first", trip_date, 9, 11)
    overlapping = itinerary_item("item_overlap", trip_date, 10, 12)
    with pytest.raises(ValidationError, match="must not overlap"):
        DayPlan(date=trip_date, items=(first, overlapping))


def test_trip_plan_rejects_missing_days_duplicate_items_and_unknown_cost_references() -> None:
    plan = three_day_plan()
    with pytest.raises(ValidationError, match="cover every trip date"):
        TripPlan.model_validate(
            {**plan.model_dump(), "days": (plan.days[0], plan.days[2], plan.days[1])}
        )

    with pytest.raises(ValidationError, match="unique across the trip"):
        duplicated_id_item = (
            plan.days[1].items[0].model_copy(update={"item_id": plan.days[0].items[0].item_id})
        )
        TripPlan.model_validate(
            {
                **plan.model_dump(),
                "days": (
                    plan.days[0],
                    plan.days[1].model_copy(update={"items": (duplicated_id_item,)}),
                    plan.days[2],
                ),
            }
        )

    with pytest.raises(ValidationError, match="unknown cost items"):
        three_day_plan(referenced_cost_id="missing_cost_item")

    with pytest.raises(ValidationError, match="unknown weather risks"):
        TripPlan.model_validate(
            {
                **plan.model_dump(),
                "days": (
                    plan.days[0],
                    plan.days[1].model_copy(update={"weather_risk_ids": ("weather_missing",)}),
                    plan.days[2],
                ),
            }
        )


def test_validation_issue_enforces_repair_and_confirmation_contracts() -> None:
    payload: dict[str, Any] = {
        "issue_id": "issue_budget_test",
        "rule_code": "budget.exceeded",
        "severity": IssueSeverity.ERROR,
        "message": "预算不足",
        "evidence": (
            ValidationEvidence(
                field_path="budget.total_limit",
                description="预算上限",
                observed_value=300,
            ),
        ),
        "responsible_node": ResponsibleNode.BUDGET,
        "repairable": True,
        "repair_action": RepairAction.NONE,
    }
    with pytest.raises(ValidationError, match="require a repair action"):
        ValidationIssue.model_validate(payload)

    with pytest.raises(ValidationError, match="requires user confirmation"):
        ValidationIssue.model_validate({**payload, "repair_action": RepairAction.ASK_USER})


def test_provider_failure_enforces_terminal_and_rate_limit_semantics() -> None:
    with pytest.raises(ValidationError, match="cannot be retryable"):
        ProviderFailure(
            provider="amap",
            operation="poi_search",
            category=ProviderErrorCategory.AUTHENTICATION_FAILED,
            message="invalid key",
            retryable=True,
        )

    with pytest.raises(ValidationError, match="only valid for rate-limited"):
        ProviderFailure(
            provider="amap",
            operation="poi_search",
            category=ProviderErrorCategory.TIMEOUT,
            message="timeout",
            retryable=True,
            retry_after_seconds=1,
        )


def test_plan_version_requires_valid_lineage_and_changed_dates() -> None:
    plan = three_day_plan()
    payload: dict[str, Any] = {
        "version_id": "version_plan_002",
        "plan": plan,
        "version_number": 2,
        "created_at": datetime(2026, 10, 1, 8, tzinfo=CHINA_TZ),
        "input_constraint_sha256": "a" * 64,
        "tool_snapshot_ids": ("snapshot_amap_001",),
        "model_versions": {"planner": "deepseek-v4-pro"},
        "prompt_versions": {"planner": "planner-v1"},
        "change_summary": ("调整第二天室外活动",),
        "changed_dates": (date(2026, 10, 3),),
    }
    with pytest.raises(ValidationError, match="require based_on_version_id"):
        PlanVersion.model_validate(payload)

    version = PlanVersion.model_validate({**payload, "based_on_version_id": "version_plan_001"})
    assert version.changed_dates == (date(2026, 10, 3),)

    with pytest.raises(ValidationError, match="within the plan date range"):
        PlanVersion.model_validate(
            {
                **payload,
                "based_on_version_id": "version_plan_001",
                "changed_dates": (date(2026, 10, 8),),
            }
        )
