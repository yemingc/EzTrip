import asyncio

import pytest
from pydantic import ValidationError

from app.evaluation import (
    PlanningMaterialEvalSuite,
    evaluate_planning_material_suite,
    load_planning_material_suite,
    planning_material_dataset_sha256,
)


def test_planning_material_suite_contract_and_hash_are_stable() -> None:
    suite = load_planning_material_suite()

    assert len(suite.cases) == 5
    assert len({item.case_id for item in suite.cases}) == 5
    assert len(planning_material_dataset_sha256(suite)) == 64


def test_fixture_suite_proves_routes_budget_failures_and_zero_call_blocking() -> None:
    report = asyncio.run(evaluate_planning_material_suite())

    assert report.passed_case_count == report.case_count == 5
    assert report.actual_edge_count == report.expected_edge_count == 42
    assert report.route_provider_call_count == 42
    assert report.typed_route_failure_count == 1
    assert report.exact_budget_case_count == 2
    assert report.blocked_zero_route_call_case_count == 1
    assert report.bounded_concurrency_case_count == 5
    assert report.source_traceability_case_count == 4


def test_suite_rejects_route_failure_expectation_without_injection() -> None:
    suite = load_planning_material_suite()
    payload = suite.model_dump(mode="json")
    payload["cases"][1]["route_failure"] = "none"

    with pytest.raises(ValidationError, match="injection must match"):
        PlanningMaterialEvalSuite.model_validate(payload)
