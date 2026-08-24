import asyncio

import pytest
from pydantic import ValidationError

from app.agents import run_plan_agent
from app.evaluation import (
    PlanAgentEvalSuite,
    PlanAgentFixtureModel,
    evaluate_plan_agent_suite,
    load_plan_agent_suite,
    plan_agent_dataset_sha256,
)


def test_plan_agent_suite_contract_and_referenced_dataset_hash_are_stable() -> None:
    suite = load_plan_agent_suite()

    assert len(suite.cases) == 6
    assert len({item.case_id for item in suite.cases}) == 6
    assert len({item.request.request_id for item in suite.cases}) == 6
    assert len(plan_agent_dataset_sha256(suite)) == 64


def test_fixture_plan_agent_suite_proves_grounding_material_use_and_zero_call_stops() -> None:
    model = PlanAgentFixtureModel()
    report = asyncio.run(
        evaluate_plan_agent_suite(
            lambda case, materials: run_plan_agent(case.request, materials, model),
            execution_mode="fixture",
            model="fixture-plan-agent-model",
        )
    )

    assert report.passed_case_count == report.case_count == 6
    assert report.planned_case_count == 5
    assert report.skipped_case_count == 1
    assert report.model_call_count == 5
    assert report.candidate_count == report.scheduled_candidate_count == 13
    assert report.grounding_rate == 1
    assert report.source_traceability_rate == 1
    assert report.route_lineage_rate == 1
    assert report.weather_preservation_rate == 1
    assert report.zero_cost_claim_case_count == 5
    assert report.skipped_zero_model_call_case_count == 1
    assert report.total_tokens == 1200


def test_plan_agent_suite_rejects_a_planned_case_with_hard_budget() -> None:
    suite = load_plan_agent_suite()
    payload = suite.model_dump(mode="json")
    payload["cases"][0]["request"]["budget"]["hard_limit"] = True

    with pytest.raises(ValidationError, match="soft budget truth boundary"):
        PlanAgentEvalSuite.model_validate(payload)
