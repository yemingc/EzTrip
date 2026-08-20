from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain import (
    BudgetCategory,
    BudgetConstraint,
    ClarificationKind,
    Constraint,
    ConstraintKind,
    ConstraintSet,
    ConstraintSource,
    ConstraintStrength,
    Party,
    PlannerCapability,
    PlannerContext,
    PlannerReadiness,
    TripRequest,
)
from app.planning import compile_planner_context


def default_budget(*, includes_lodging: bool = False) -> BudgetConstraint:
    categories = (
        BudgetCategory.LODGING,
        BudgetCategory.TRANSPORT,
        BudgetCategory.FOOD,
        BudgetCategory.ADMISSION,
        BudgetCategory.ACTIVITY,
    )
    if not includes_lodging:
        categories = categories[1:]
    return BudgetConstraint(
        total_limit="3000.00",
        included_categories=categories,
    )


def confirmed_constraints() -> ConstraintSet:
    return ConstraintSet(
        items=(
            Constraint(
                constraint_id="must_visit_forbidden_city",
                kind=ConstraintKind.MUST_VISIT,
                value="故宫",
                strength=ConstraintStrength.HARD,
                priority=5,
                source=ConstraintSource.USER_EXPLICIT,
                confirmed=True,
            ),
            Constraint(
                constraint_id="walking_intensity_low",
                kind=ConstraintKind.WALKING_INTENSITY,
                value="low",
                strength=ConstraintStrength.SOFT,
                priority=4,
                source=ConstraintSource.USER_EXPLICIT,
                confirmed=True,
            ),
            Constraint(
                constraint_id="museum_on_day_two",
                kind=ConstraintKind.SCHEDULE,
                value="第二天安排博物馆",
                strength=ConstraintStrength.HARD,
                priority=4,
                source=ConstraintSource.USER_CONFIRMED,
                applies_to_dates=(date(2026, 10, 3),),
                confirmed=True,
            ),
        )
    )


def make_request(
    *,
    destination_city: str = "北京",
    rooms: int | None = 1,
    budget: BudgetConstraint | None = None,
    use_default_budget: bool = True,
    constraints: ConstraintSet | None = None,
) -> TripRequest:
    selected_budget = default_budget() if use_default_budget else budget
    return TripRequest(
        request_id="request_beijing_context",
        raw_text="两位成年人北京三日游, 必须去故宫, 偏好轻步行。",
        origin_city="上海市",
        destination_city=destination_city,
        start_date=date(2026, 10, 2),
        end_date=date(2026, 10, 4),
        party=Party(adults=2, rooms=rooms),
        budget=selected_budget,
        travel_styles=("历史文化", "轻步行"),
        constraints=constraints or confirmed_constraints(),
    )


def test_compiler_derives_dates_party_budget_and_constraint_scopes() -> None:
    context = compile_planner_context(make_request())

    assert context.destination.model_dump() == {
        "input_name": "北京",
        "normalized_name": "北京市",
        "administrative_code": "110000",
        "primary_provider_supported": True,
    }
    assert context.day_count == 3
    assert context.lodging_nights == 2
    assert context.party.total_travelers == 2
    assert context.party.rooms == 1
    assert context.party.room_nights == 2

    assert context.budget is not None
    assert context.budget.total_limit == Decimal("3000.00")
    assert context.budget.includes_lodging is False
    assert context.budget.reference_party_per_day == Decimal("1000.00")
    assert context.budget.reference_per_traveler_trip == Decimal("1500.00")
    assert context.budget.reference_per_traveler_day == Decimal("500.00")
    assert context.budget.excluded_categories == (
        BudgetCategory.LODGING,
        BudgetCategory.OTHER,
    )

    assert [item.constraint_id for item in context.confirmed_hard_constraints] == [
        "must_visit_forbidden_city",
        "museum_on_day_two",
    ]
    assert [item.constraint_id for item in context.confirmed_soft_constraints] == [
        "walking_intensity_low"
    ]
    assert context.pending_constraints == ()
    assert context.global_constraint_ids == (
        "must_visit_forbidden_city",
        "walking_intensity_low",
    )
    assert [day.constraint_ids for day in context.days] == [
        (),
        ("museum_on_day_two",),
        (),
    ]
    assert context.readiness == PlannerReadiness.READY
    assert context.clarifications == ()
    assert context.blocked_capabilities == ()
    assert set(context.ready_capabilities) == set(PlannerCapability)


def test_missing_budget_is_a_nonblocking_question_but_budget_validation_is_unavailable() -> None:
    context = compile_planner_context(make_request(use_default_budget=False, budget=None))

    assert context.budget is None
    assert context.readiness == PlannerReadiness.READY_WITH_QUESTIONS
    assert [item.kind for item in context.clarifications] == [ClarificationKind.MISSING_BUDGET]
    assert context.clarifications[0].blocking is False
    assert context.blocked_capabilities == (PlannerCapability.BUDGET_VALIDATION,)
    assert PlannerCapability.CANDIDATE_SEARCH in context.ready_capabilities
    assert PlannerCapability.PLAN_FINALIZATION in context.ready_capabilities


def test_missing_rooms_blocks_lodging_budget_and_finalization_when_lodging_is_in_scope() -> None:
    context = compile_planner_context(
        make_request(
            rooms=None,
            use_default_budget=False,
            budget=default_budget(includes_lodging=True),
        )
    )

    assert context.party.room_nights is None
    assert context.readiness == PlannerReadiness.NEEDS_CLARIFICATION
    assert [item.kind for item in context.clarifications] == [ClarificationKind.MISSING_ROOMS]
    assert context.clarifications[0].blocking is True
    assert set(context.blocked_capabilities) == {
        PlannerCapability.STAY_SEARCH,
        PlannerCapability.BUDGET_VALIDATION,
        PlannerCapability.PLAN_FINALIZATION,
    }


def test_missing_rooms_does_not_block_local_plan_when_lodging_is_excluded() -> None:
    context = compile_planner_context(make_request(rooms=None))

    assert context.readiness == PlannerReadiness.READY_WITH_QUESTIONS
    assert context.clarifications[0].kind == ClarificationKind.MISSING_ROOMS
    assert context.clarifications[0].blocking is False
    assert context.blocked_capabilities == (PlannerCapability.STAY_SEARCH,)
    assert PlannerCapability.BUDGET_VALIDATION in context.ready_capabilities
    assert PlannerCapability.PLAN_FINALIZATION in context.ready_capabilities


@pytest.mark.parametrize(
    ("strength,expected_readiness,finalization_blocked"),
    [
        (ConstraintStrength.HARD, PlannerReadiness.NEEDS_CLARIFICATION, True),
        (ConstraintStrength.SOFT, PlannerReadiness.READY_WITH_QUESTIONS, False),
    ],
)
def test_unconfirmed_constraints_preserve_strength_without_silent_promotion(
    strength: ConstraintStrength,
    expected_readiness: PlannerReadiness,
    finalization_blocked: bool,
) -> None:
    pending = Constraint(
        constraint_id="pending_early_start",
        kind=ConstraintKind.SCHEDULE,
        value="每天七点出发",
        strength=strength,
        priority=3,
        source=ConstraintSource.AGENT_INFERRED,
        confirmed=False,
    )
    context = compile_planner_context(make_request(constraints=ConstraintSet(items=(pending,))))

    assert context.pending_constraints == (pending,)
    assert context.confirmed_hard_constraints == ()
    assert context.confirmed_soft_constraints == ()
    assert context.readiness == expected_readiness
    assert context.clarifications[0].kind == ClarificationKind.UNCONFIRMED_CONSTRAINT
    assert context.clarifications[0].constraint_id == pending.constraint_id
    assert (
        PlannerCapability.PLAN_FINALIZATION in context.blocked_capabilities
    ) is finalization_blocked


def test_unsupported_destination_blocks_provider_dependent_capabilities() -> None:
    context = compile_planner_context(make_request(destination_city="南京市"))

    assert context.destination.primary_provider_supported is False
    assert context.destination.administrative_code is None
    assert context.readiness == PlannerReadiness.NEEDS_CLARIFICATION
    assert context.clarifications[0].kind == ClarificationKind.UNSUPPORTED_DESTINATION
    assert set(context.blocked_capabilities) == {
        PlannerCapability.CANDIDATE_SEARCH,
        PlannerCapability.STAY_SEARCH,
        PlannerCapability.WEATHER_LOOKUP,
        PlannerCapability.ROUTE_PLANNING,
        PlannerCapability.PLAN_FINALIZATION,
    }
    assert context.ready_capabilities == (PlannerCapability.BUDGET_VALIDATION,)


def test_compilation_is_deterministic_and_input_hash_changes_with_semantics() -> None:
    request = make_request()
    first = compile_planner_context(request)
    second = compile_planner_context(request.model_copy(deep=True))
    changed = compile_planner_context(
        request.model_copy(
            update={
                "budget": BudgetConstraint(
                    total_limit="3500.00",
                    included_categories=request.budget.included_categories,
                )
            }
        )
    )

    assert first == second
    assert first.context_id == second.context_id
    assert first.input_request_sha256 == second.input_request_sha256
    assert changed.context_id != first.context_id
    assert changed.input_request_sha256 != first.input_request_sha256


def test_planner_context_rejects_inconsistent_derived_values() -> None:
    context = compile_planner_context(make_request())
    payload = context.model_dump(mode="json")

    with pytest.raises(ValidationError, match="day_count"):
        PlannerContext.model_validate({**payload, "day_count": 2})
