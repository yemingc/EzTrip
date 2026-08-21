import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.domain import (
    MinimalPlanningResult,
    PlannerContext,
    PlanValidationReport,
    TripPlan,
    TripRequest,
    ValidationIssue,
)
from app.planning import compile_planner_context
from scripts.export_domain_schemas import DEFAULT_OUTPUT_PATH, SCHEMA_MODELS, build_schema_bundle
from scripts.export_plan_validation_example import (
    DEFAULT_OUTPUT_PATH as PLAN_VALIDATION_EXAMPLE_PATH,
)
from scripts.export_plan_validation_example import build_plan_validation_example
from scripts.export_planner_context_example import (
    DEFAULT_OUTPUT_PATH as PLANNER_CONTEXT_EXAMPLE_PATH,
)
from scripts.run_minimal_planning_graph import (
    DEFAULT_OUTPUT_PATH as MINIMAL_PLANNING_RESULT_EXAMPLE_PATH,
)
from scripts.run_minimal_planning_graph import build_fixture_result

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIRECTORY = REPOSITORY_ROOT / "docs" / "contracts" / "examples"


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_trip_request_example_is_valid_and_preserves_budget_semantics() -> None:
    request = TripRequest.model_validate(load_json(EXAMPLE_DIRECTORY / "trip-request.v1.json"))

    assert request.destination_city == "北京市"
    assert request.day_count == 3
    assert request.lodging_nights == 2
    assert request.party.total_travelers == 2
    assert request.budget is not None
    assert request.budget.total_limit == Decimal("3000.00")
    assert request.budget.includes_lodging is False
    assert TripRequest.model_validate_json(request.model_dump_json()) == request


def test_planner_context_example_is_valid_and_reproducible_from_trip_request() -> None:
    request = TripRequest.model_validate(load_json(EXAMPLE_DIRECTORY / "trip-request.v1.json"))
    committed_context = PlannerContext.model_validate(load_json(PLANNER_CONTEXT_EXAMPLE_PATH))

    assert committed_context == compile_planner_context(request)
    assert committed_context.request_id == request.request_id
    assert committed_context.day_count == 3
    assert committed_context.lodging_nights == 2
    assert committed_context.readiness == "ready"
    assert committed_context.blocked_capabilities == ()
    assert PlannerContext.model_validate_json(committed_context.model_dump_json()) == (
        committed_context
    )


def test_minimal_planning_result_example_replays_the_fixture_graph() -> None:
    committed_result = MinimalPlanningResult.model_validate(
        load_json(MINIMAL_PLANNING_RESULT_EXAMPLE_PATH)
    )
    replayed_result = asyncio.run(build_fixture_result())

    assert committed_result == replayed_result
    assert committed_result.status == "candidates_ready"
    assert [candidate.name for candidate in committed_result.candidates] == ["故宫博物院"]
    assert committed_result.candidates[0].source.data_mode == "fixture"
    assert committed_result.provider_failures == ()


def test_trip_plan_example_is_valid_recalculable_and_source_traceable() -> None:
    plan = TripPlan.model_validate(load_json(EXAMPLE_DIRECTORY / "trip-plan.v1.json"))

    assert len(plan.days) == 3
    assert plan.total_cost_minimum == Decimal("120.00")
    assert plan.total_cost_maximum == Decimal("120.00")
    assert plan.days[1].weather_risk_ids[0] == plan.weather_risks[0].risk_id
    grounded_items = [item for day in plan.days for item in day.items if item.candidate_id]
    assert grounded_items
    assert all(item.source and item.source.provider_id for item in grounded_items)
    assert TripPlan.model_validate_json(plan.model_dump_json()) == plan


def test_validation_issue_example_requires_explicit_user_approval() -> None:
    issue = ValidationIssue.model_validate(
        load_json(EXAMPLE_DIRECTORY / "validation-issue.v1.json")
    )

    assert issue.responsible_node == "budget"
    assert issue.repair_action == "ask_user"
    assert issue.requires_user_confirmation is True
    assert ValidationIssue.model_validate_json(issue.model_dump_json()) == issue


def test_plan_validation_example_is_reproducible_and_rejects_missing_cost_scope() -> None:
    committed_report = PlanValidationReport.model_validate(load_json(PLAN_VALIDATION_EXAMPLE_PATH))
    replayed_report = build_plan_validation_example()

    assert committed_report == replayed_report
    assert committed_report.status == "conflicted"
    assert committed_report.can_finalize is False
    assert committed_report.budget.status == "incomplete"
    assert {item.value for item in committed_report.budget.missing_categories} == {
        "transport",
        "food",
        "activity",
    }
    assert {item.rule_code for item in committed_report.issues} == {
        "budget.incomplete_category_coverage"
    }


def test_committed_schema_bundle_matches_the_pydantic_models() -> None:
    committed_bundle = load_json(DEFAULT_OUTPUT_PATH)
    generated_bundle = build_schema_bundle()

    assert committed_bundle == generated_bundle
    assert set(committed_bundle["models"]) == {model.__name__ for model in SCHEMA_MODELS}
    assert committed_bundle["version"] == "1.0"
