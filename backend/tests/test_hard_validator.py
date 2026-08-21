import asyncio
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from app.agents.plan_agent import run_plan_agent
from app.domain import (
    Constraint,
    ConstraintKind,
    ConstraintSet,
    ConstraintSource,
    ConstraintStrength,
    DataMode,
    OpeningHoursEvidence,
    OpeningHoursEvidenceBundle,
    SourceReference,
    TripPlan,
    TripRequest,
)
from app.evaluation.plan_agent import (
    PlanAgentFixtureModel,
    build_plan_agent_materials,
    load_plan_agent_suite,
)
from app.planning import HARD_VALIDATOR_VERSION, validate_hard_trip_plan


def _request_with_constraint(kind: ConstraintKind, value: str) -> TripRequest:
    request = load_plan_agent_suite().cases[0].request
    constraint = Constraint(
        constraint_id=f"hard_{kind.value}_fixture",
        kind=kind,
        value=value,
        strength=ConstraintStrength.HARD,
        priority=5,
        source=ConstraintSource.USER_EXPLICIT,
        confirmed=True,
    )
    return TripRequest.model_validate(
        {
            **request.model_dump(mode="python"),
            "constraints": ConstraintSet(items=(constraint,)),
        }
    )


def _build_plan(request: TripRequest | None = None):
    source_case = load_plan_agent_suite().cases[0]
    case = (
        source_case.model_copy(update={"request": request}) if request is not None else source_case
    )
    materials = asyncio.run(build_plan_agent_materials(case))
    result = run_plan_agent(case.request, materials, PlanAgentFixtureModel())
    assert result.plan is not None
    return case.request, materials, result.plan


def _opening_hours(request: TripRequest, plan: TripPlan) -> OpeningHoursEvidenceBundle:
    items = tuple(
        OpeningHoursEvidence(
            evidence_id=f"opening-{item.item_id}",
            candidate_id=item.candidate_id,
            service_date=day.date,
            opens_at=item.start_at - timedelta(hours=1),
            closes_at=item.end_at + timedelta(hours=1),
            source=SourceReference(
                provider="opening-hours-fixture",
                provider_id=f"opening-source-{item.item_id}",
                data_mode=DataMode.FIXTURE,
                retrieved_at=datetime(2026, 9, 20, tzinfo=item.start_at.tzinfo),
            ),
        )
        for day in plan.days
        for item in day.items
        if item.candidate_id is not None
    )
    return OpeningHoursEvidenceBundle(
        request_id=request.request_id,
        data_mode=DataMode.FIXTURE,
        items=items,
    )


def _rule_codes(report) -> set[str]:
    return {item.rule_code for item in report.issues}


def test_complete_grounded_draft_passes_hard_rules_but_keeps_soft_budget_warning() -> None:
    request, materials, plan = _build_plan()

    first = validate_hard_trip_plan(request, plan, materials, _opening_hours(request, plan))
    second = validate_hard_trip_plan(request, plan, materials, _opening_hours(request, plan))

    assert first == second
    assert first.validator_version == HARD_VALIDATOR_VERSION
    assert first.status == "warning"
    assert first.can_finalize is True
    assert _rule_codes(first) == {"budget.incomplete_category_coverage"}
    assert "route.transfer_windows_feasible" in first.passed_rule_codes
    assert "opening_hours.evidence_complete" in first.passed_rule_codes


@pytest.mark.parametrize(
    ("kind", "value", "expected_rule"),
    [
        (ConstraintKind.MUST_VISIT, "颐和园", "constraint.hard_must_visit_missing"),
        (ConstraintKind.AVOID, "故宫博物院", "constraint.hard_avoid_scheduled"),
    ],
)
def test_confirmed_hard_must_and_avoid_constraints_are_mechanically_enforced(
    kind: ConstraintKind,
    value: str,
    expected_rule: str,
) -> None:
    request, materials, plan = _build_plan(_request_with_constraint(kind, value))

    report = validate_hard_trip_plan(request, plan, materials, _opening_hours(request, plan))

    assert report.status == "conflicted"
    assert report.can_finalize is False
    assert expected_rule in _rule_codes(report)
    issue = next(item for item in report.issues if item.rule_code == expected_rule)
    assert issue.responsible_node == "explore"
    assert issue.repair_action == "rerun_explore"


def test_missing_route_is_routed_to_route_node() -> None:
    request, materials, plan = _build_plan()
    payload = plan.model_dump(mode="python")
    payload["days"][0]["items"][0]["route_from_previous"] = None
    broken_plan = TripPlan.model_validate(payload)

    report = validate_hard_trip_plan(
        request,
        broken_plan,
        materials,
        _opening_hours(request, broken_plan),
    )

    assert "route.missing_for_grounded_item" in _rule_codes(report)
    issue = next(
        item for item in report.issues if item.rule_code == "route.missing_for_grounded_item"
    )
    assert issue.responsible_node == "route"
    assert issue.repair_action == "rerun_route"


def test_insufficient_transfer_time_is_routed_to_plan_node() -> None:
    request, materials, plan = _build_plan()
    payload = plan.model_dump(mode="python")
    two_item_day = next(day for day in payload["days"] if len(day["items"]) == 2)
    first, second = two_item_day["items"]
    second["start_at"] = first["end_at"] + timedelta(minutes=30)
    second["end_at"] = second["start_at"] + timedelta(hours=2)
    broken_plan = TripPlan.model_validate(payload)

    report = validate_hard_trip_plan(
        request,
        broken_plan,
        materials,
        _opening_hours(request, broken_plan),
    )

    assert "route.insufficient_transfer_window" in _rule_codes(report)
    issue = next(
        item for item in report.issues if item.rule_code == "route.insufficient_transfer_window"
    )
    assert issue.responsible_node == "plan"
    assert issue.repair_action == "replan_day"


def test_missing_opening_hours_evidence_blocks_finalization() -> None:
    request, materials, plan = _build_plan()
    complete = _opening_hours(request, plan)
    incomplete = complete.model_copy(update={"items": complete.items[1:]})

    report = validate_hard_trip_plan(request, plan, materials, incomplete)

    assert report.status == "conflicted"
    assert report.can_finalize is False
    assert "opening_hours.evidence_missing" in _rule_codes(report)


def test_opening_hours_contract_rejects_estimated_or_user_input_facts() -> None:
    request, _, plan = _build_plan()
    payload = _opening_hours(request, plan).items[0].model_dump(mode="python")
    payload["source"] = {
        **payload["source"],
        "data_mode": DataMode.ESTIMATE,
    }

    with pytest.raises(ValidationError, match="live or fixture"):
        OpeningHoursEvidence.model_validate(payload)
