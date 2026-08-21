import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain import (
    ActivityKind,
    BudgetCategory,
    BudgetConstraint,
    CostItem,
    DataMode,
    DayPlan,
    ItineraryItem,
    MoneyRange,
    Party,
    PlanStatus,
    PlanValidationReport,
    SourceReference,
    TripPlan,
    TripRequest,
)
from app.planning import VALIDATOR_VERSION, validate_trip_plan

CHINA_TZ = timezone(timedelta(hours=8))
START_DATE = date(2026, 10, 2)


def source_reference(
    provider_id: str,
    *,
    data_mode: DataMode = DataMode.FIXTURE,
) -> SourceReference:
    return SourceReference(
        provider="validator_fixture",
        provider_id=provider_id,
        data_mode=data_mode,
        retrieved_at=datetime(2026, 9, 20, 8, tzinfo=CHINA_TZ),
    )


def cost_item(
    cost_id: str,
    category: BudgetCategory,
    minimum: str,
    maximum: str,
    *,
    quantity: str = "1",
) -> CostItem:
    return CostItem(
        cost_item_id=cost_id,
        category=category,
        description=f"{category.value} fixture cost",
        quantity=quantity,
        unit_price=MoneyRange(minimum=minimum, maximum=maximum),
        source=source_reference(f"source_{cost_id}"),
        is_estimate=minimum != maximum,
    )


def trip_request(
    *,
    limit: str | None = "1000",
    categories: tuple[BudgetCategory, ...] = (BudgetCategory.ADMISSION,),
    hard_limit: bool = True,
    request_id: str = "request_validator",
    destination_city: str = "北京市",
    start_date: date = START_DATE,
) -> TripRequest:
    budget = None
    if limit is not None:
        budget = BudgetConstraint(
            total_limit=limit,
            included_categories=categories,
            hard_limit=hard_limit,
        )
    return TripRequest(
        request_id=request_id,
        raw_text="验证一个结构化两日旅行计划",
        destination_city=destination_city,
        start_date=start_date,
        end_date=start_date + timedelta(days=1),
        party=Party(adults=2),
        budget=budget,
    )


def itinerary_item(
    item_id: str,
    day: date,
    candidate_id: str,
    *,
    data_mode: DataMode = DataMode.FIXTURE,
) -> ItineraryItem:
    return ItineraryItem(
        item_id=item_id,
        kind=ActivityKind.ATTRACTION,
        title=f"景点 {candidate_id}",
        start_at=datetime(day.year, day.month, day.day, 9, tzinfo=CHINA_TZ),
        end_at=datetime(day.year, day.month, day.day, 11, tzinfo=CHINA_TZ),
        candidate_id=candidate_id,
        source=source_reference(f"source_{item_id}", data_mode=data_mode),
    )


def trip_plan(
    *,
    costs: tuple[CostItem, ...] = (),
    candidate_ids: tuple[str, str] = ("candidate_one", "candidate_two"),
    source_mode: DataMode = DataMode.FIXTURE,
    request_id: str = "request_validator",
    destination_city: str = "北京市",
    status: PlanStatus = PlanStatus.DRAFT,
) -> TripPlan:
    days = tuple(
        DayPlan(
            date=START_DATE + timedelta(days=offset),
            items=(
                itinerary_item(
                    f"item_day_{offset + 1}",
                    START_DATE + timedelta(days=offset),
                    candidate_ids[offset],
                    data_mode=source_mode,
                ),
            ),
        )
        for offset in range(2)
    )
    return TripPlan(
        plan_id="plan_validator",
        request_id=request_id,
        status=status,
        destination_city=destination_city,
        start_date=START_DATE,
        end_date=START_DATE + timedelta(days=1),
        days=days,
        cost_items=costs,
    )


def rule_codes(report: PlanValidationReport) -> set[str]:
    return {item.rule_code for item in report.issues}


def test_within_budget_plan_passes_with_recalculated_decimal_totals() -> None:
    request = trip_request(limit="1000")
    plan = trip_plan(
        costs=(
            cost_item(
                "cost_ticket",
                BudgetCategory.ADMISSION,
                "60.00",
                "60.00",
                quantity="2",
            ),
        )
    )

    first = validate_trip_plan(request, plan)
    second = validate_trip_plan(request, plan)

    assert first == second
    assert first.validator_version == VALIDATOR_VERSION
    assert first.status == "passed"
    assert first.can_finalize is True
    assert first.issues == ()
    assert first.budget.total_minimum == Decimal("120.00")
    assert first.budget.total_maximum == Decimal("120.00")
    assert first.budget.status == "within_limit"


def test_hard_budget_floor_exceeded_returns_conflict_and_exact_gap() -> None:
    request = trip_request(limit="1000")
    plan = trip_plan(
        costs=(
            cost_item(
                "cost_ticket",
                BudgetCategory.ADMISSION,
                "600.00",
                "600.00",
                quantity="2",
            ),
        )
    )

    report = validate_trip_plan(request, plan)

    assert report.status == "conflicted"
    assert report.can_finalize is False
    assert report.budget.status == "exceeded"
    assert report.budget.minimum_gap == Decimal("200.00")
    assert rule_codes(report) == {"budget.deterministic_floor_exceeds_limit"}
    issue = report.issues[0]
    assert issue.repair_action == "ask_user"
    assert issue.requires_user_confirmation is True


@pytest.mark.parametrize(
    ("hard_limit", "expected_status", "can_finalize"),
    [(True, "conflicted", False), (False, "warning", True)],
)
def test_budget_range_overrun_respects_hard_or_soft_limit(
    hard_limit: bool,
    expected_status: str,
    can_finalize: bool,
) -> None:
    request = trip_request(limit="1000", hard_limit=hard_limit)
    plan = trip_plan(
        costs=(
            cost_item(
                "cost_variable",
                BudgetCategory.ADMISSION,
                "400.00",
                "600.00",
                quantity="2",
            ),
        )
    )

    report = validate_trip_plan(request, plan)

    assert report.status == expected_status
    assert report.can_finalize is can_finalize
    assert report.budget.status == "possible_overrun"
    assert report.budget.maximum_gap == Decimal("200.00")
    assert rule_codes(report) == {"budget.possible_overrun"}


def test_missing_budget_category_is_not_silently_treated_as_zero() -> None:
    request = trip_request(
        categories=(BudgetCategory.ADMISSION, BudgetCategory.FOOD),
    )
    plan = trip_plan(costs=(cost_item("cost_ticket", BudgetCategory.ADMISSION, "120", "120"),))

    report = validate_trip_plan(request, plan)

    assert report.status == "conflicted"
    assert report.budget.status == "incomplete"
    assert report.budget.missing_categories == (BudgetCategory.FOOD,)
    assert rule_codes(report) == {"budget.incomplete_category_coverage"}


def test_budget_scope_excludes_out_of_scope_costs_without_losing_them() -> None:
    request = trip_request(categories=(BudgetCategory.ADMISSION,))
    admission = cost_item("cost_ticket", BudgetCategory.ADMISSION, "120", "120")
    lodging = cost_item("cost_hotel", BudgetCategory.LODGING, "500", "500")

    report = validate_trip_plan(request, trip_plan(costs=(admission, lodging)))

    assert report.status == "passed"
    assert report.budget.total_minimum == Decimal("120")
    assert report.budget.considered_cost_item_ids == ("cost_ticket",)
    assert report.budget.excluded_cost_item_ids == ("cost_hotel",)


def test_no_budget_request_makes_no_budget_guarantee() -> None:
    report = validate_trip_plan(
        trip_request(limit=None),
        trip_plan(costs=(cost_item("cost_ticket", BudgetCategory.ADMISSION, "120", "120"),)),
    )

    assert report.status == "passed"
    assert report.budget.status == "not_requested"
    assert report.budget.total_limit is None
    assert report.budget.considered_cost_item_ids == ()
    assert report.budget.excluded_cost_item_ids == ("cost_ticket",)


@pytest.mark.parametrize(
    ("request_input", "plan_input", "expected_rule"),
    [
        (
            trip_request(request_id="request_expected"),
            trip_plan(request_id="request_other"),
            "plan.request_mismatch",
        ),
        (
            trip_request(destination_city="北京市"),
            trip_plan(destination_city="上海市"),
            "plan.destination_mismatch",
        ),
        (
            trip_request(start_date=START_DATE + timedelta(days=1)),
            trip_plan(),
            "plan.date_window_mismatch",
        ),
    ],
)
def test_cross_object_identity_city_and_date_conflicts_are_reported(
    request_input: TripRequest,
    plan_input: TripPlan,
    expected_rule: str,
) -> None:
    report = validate_trip_plan(request_input, plan_input)

    assert report.status == "conflicted"
    assert expected_rule in rule_codes(report)


def test_duplicate_candidate_is_rejected_even_when_item_ids_are_unique() -> None:
    report = validate_trip_plan(
        trip_request(),
        trip_plan(candidate_ids=("candidate_same", "candidate_same")),
    )

    assert report.status == "conflicted"
    assert "plan.duplicate_candidate" in rule_codes(report)
    assert report.issues[0].repair_action == "replan_day"


def test_grounded_recommendation_rejects_user_input_or_estimate_source_mode() -> None:
    report = validate_trip_plan(
        trip_request(),
        trip_plan(source_mode=DataMode.USER_INPUT),
    )

    assert report.status == "conflicted"
    assert "source.invalid_grounding_mode" in rule_codes(report)
    assert any(item.repair_action == "rerun_explore" for item in report.issues)


def test_final_plan_with_error_gets_an_explicit_finalization_issue() -> None:
    request = trip_request(limit="1000")
    plan = trip_plan(
        status=PlanStatus.FINAL,
        costs=(
            cost_item(
                "cost_ticket",
                BudgetCategory.ADMISSION,
                "1200",
                "1200",
            ),
        ),
    )

    report = validate_trip_plan(request, plan)

    assert report.can_finalize is False
    assert rule_codes(report) == {
        "budget.deterministic_floor_exceeds_limit",
        "plan.finalized_with_errors",
    }


def test_preexisting_conflicted_plan_cannot_finalize_even_if_rules_otherwise_pass() -> None:
    request = trip_request(limit="1000")
    plan = trip_plan(
        status=PlanStatus.CONFLICTED,
        costs=(cost_item("cost_ticket", BudgetCategory.ADMISSION, "120", "120"),),
    )

    report = validate_trip_plan(request, plan)

    assert report.status == "conflicted"
    assert report.can_finalize is False
    assert rule_codes(report) == {"plan.preexisting_conflicted_status"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "passed"),
        ("can_finalize", True),
        ("passed_rule_codes", ["budget.deterministic_floor_exceeds_limit"]),
    ],
)
def test_report_contract_rejects_inconsistent_status_or_rule_partition(
    field: str,
    value: object,
) -> None:
    request = trip_request(limit="1000")
    plan = trip_plan(costs=(cost_item("cost_ticket", BudgetCategory.ADMISSION, "1200", "1200"),))
    payload = copy.deepcopy(validate_trip_plan(request, plan).model_dump(mode="json"))
    payload[field] = value

    with pytest.raises(ValidationError):
        PlanValidationReport.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "exceeded"), ("minimum_gap", "1.00")],
)
def test_budget_summary_contract_rejects_inconsistent_status_or_gap(
    field: str,
    value: object,
) -> None:
    report = validate_trip_plan(
        trip_request(limit="1000"),
        trip_plan(costs=(cost_item("cost_ticket", BudgetCategory.ADMISSION, "120", "120"),)),
    )
    payload = copy.deepcopy(report.model_dump(mode="json"))
    payload["budget"][field] = value

    with pytest.raises(ValidationError):
        PlanValidationReport.model_validate(payload)
