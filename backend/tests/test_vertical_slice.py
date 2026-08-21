import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.agents.contracts import PlannerModelResponse
from app.domain.request import ConstraintSet
from app.domain.sources import DataMode
from app.domain.workflow import PlanningWorkflowStatus
from app.evaluation import (
    VerticalSliceCase,
    VerticalSliceGateReport,
    VerticalSliceSuite,
    evaluate_vertical_slice_suite,
    load_vertical_slice_suite,
    run_vertical_slice_case,
)
from app.evaluation.vertical_slice import (
    VERTICAL_SLICE_NORMAL_RESULT_PATH,
    VERTICAL_SLICE_REPORT_PATH,
    VERTICAL_SLICE_SUITE_PATH,
    FixturePlannerProposalModel,
    VerticalSliceEvaluationError,
    VerticalSliceScenarioProvider,
)
from app.planning import (
    VerticalSliceProtocolError,
    VerticalSliceResult,
    assemble_trip_plan,
    run_trip_planning_vertical_slice,
)
from app.providers import POISearchRequest
from scripts.export_vertical_slice_schemas import (
    REPORT_SCHEMA_PATH,
    SUITE_SCHEMA_PATH,
    build_vertical_slice_report_schema,
    build_vertical_slice_suite_schema,
)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_gate2_suite_and_report_match_generated_json_schemas() -> None:
    suite_payload = load_json(VERTICAL_SLICE_SUITE_PATH)
    report_payload = load_json(VERTICAL_SLICE_REPORT_PATH)
    committed_suite_schema = load_json(SUITE_SCHEMA_PATH)
    committed_report_schema = load_json(REPORT_SCHEMA_PATH)

    assert committed_suite_schema == build_vertical_slice_suite_schema()
    assert committed_report_schema == build_vertical_slice_report_schema()
    Draft202012Validator.check_schema(committed_suite_schema)
    Draft202012Validator.check_schema(committed_report_schema)
    assert not list(Draft202012Validator(committed_suite_schema).iter_errors(suite_payload))
    assert not list(Draft202012Validator(committed_report_schema).iter_errors(report_payload))
    assert VerticalSliceSuite.model_validate(suite_payload).suite == (
        "beijing-three-day-vertical-slice-gate2-v1"
    )
    assert VerticalSliceGateReport.model_validate(report_payload).case_count == 2


def test_gate2_report_and_full_normal_result_are_mechanically_replayable() -> None:
    committed_report = VerticalSliceGateReport.model_validate(load_json(VERTICAL_SLICE_REPORT_PATH))
    replayed_report = asyncio.run(evaluate_vertical_slice_suite())
    suite = load_vertical_slice_suite()
    normal_case = next(case for case in suite.cases if case.expected.outcome == "ready")
    replayed_normal, _ = asyncio.run(run_vertical_slice_case(normal_case))
    committed_normal = VerticalSliceResult.model_validate(
        load_json(VERTICAL_SLICE_NORMAL_RESULT_PATH)
    )

    assert committed_report == replayed_report
    assert committed_normal == replayed_normal
    assert replayed_report.passed_case_count == 2
    assert replayed_report.passed_check_count == 20
    assert replayed_report.traceable_candidate_count == 6
    assert replayed_report.deterministic_replay_count == 2


def test_normal_case_outputs_three_grounded_days_and_recomputed_budget() -> None:
    suite = load_vertical_slice_suite()
    case = next(item for item in suite.cases if item.expected.outcome == "ready")
    result, provider_calls = asyncio.run(run_vertical_slice_case(case))

    assert len(provider_calls) == 3
    assert result.outcome == "ready"
    assert result.validation.status == "passed"
    assert result.validation.can_finalize is True
    assert len(result.plan.days) == 3
    assert all(len(day.items) == 1 for day in result.plan.days)
    assert result.plan.total_cost_minimum == Decimal("500.00")
    assert result.validation.budget.total_minimum == result.plan.total_cost_minimum
    assert all(
        item.source is not None and item.source.provider_id
        for day in result.plan.days
        for item in day.items
    )


def test_budget_conflict_preserves_candidates_and_blocks_finalization() -> None:
    suite = load_vertical_slice_suite()
    case = next(item for item in suite.cases if item.expected.outcome == "conflicted")
    result, _ = asyncio.run(run_vertical_slice_case(case))

    upstream_ids = {item.candidate_id for item in result.upstream.candidates}
    scheduled_ids = {
        item.candidate_id
        for day in result.plan.days
        for item in day.items
        if item.candidate_id is not None
    }
    assert scheduled_ids == upstream_ids
    assert result.outcome == "conflicted"
    assert result.plan.status == "draft"
    assert result.validation.can_finalize is False
    assert result.validation.budget.total_minimum == Decimal("900.00")
    assert result.validation.budget.minimum_gap == Decimal("600.00")
    assert {item.rule_code for item in result.validation.issues} == {
        "budget.deterministic_floor_exceeds_limit"
    }


def test_assembler_rejects_partial_day_plans_at_vertical_slice_boundary() -> None:
    suite = load_vertical_slice_suite()
    case = next(item for item in suite.cases if item.expected.outcome == "ready")
    result, _ = asyncio.run(run_vertical_slice_case(case))
    partial_planner = result.planner.model_copy(update={"day_plans": result.planner.day_plans[:2]})

    with pytest.raises(VerticalSliceProtocolError, match="every trip date"):
        assemble_trip_plan(
            case.request,
            result.upstream,
            partial_planner,
            case.cost_items,
        )


def test_scenario_provider_rejects_undeclared_search_call() -> None:
    case = load_vertical_slice_suite().cases[0]
    provider = VerticalSliceScenarioProvider(case.provider_responses)

    with pytest.raises(VerticalSliceEvaluationError, match="call mismatch"):
        asyncio.run(
            provider.search_pois(
                POISearchRequest(keywords="未声明景点", city_adcode="110000", limit=1)
            )
        )
    with pytest.raises(VerticalSliceEvaluationError, match="did not receive every"):
        provider.verify_complete()


def test_case_contract_rejects_planner_candidate_not_from_provider() -> None:
    payload = load_json(VERTICAL_SLICE_SUITE_PATH)["cases"][0]
    payload = copy.deepcopy(payload)
    payload["planner_proposal"]["items"][0]["candidate_id"] = "untrusted-candidate"

    with pytest.raises(ValidationError, match="cover the provider candidates exactly"):
        VerticalSliceCase.model_validate(payload)


def test_report_contract_rejects_inconsistent_aggregate() -> None:
    payload = load_json(VERTICAL_SLICE_REPORT_PATH)
    payload["passed_check_count"] = 19

    with pytest.raises(ValidationError, match="passed_check_count"):
        VerticalSliceGateReport.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("request_id", "preserve one request_id"),
        ("data_mode", "data_mode must match"),
        ("context_id", "preserve the upstream context_id"),
        ("plan_id", "reference the assembled plan"),
        ("outcome", "outcome must follow"),
        ("final_status", "cannot auto-finalize"),
    ],
)
def test_vertical_slice_result_rejects_broken_stage_lineage(
    mutation: str,
    expected_error: str,
) -> None:
    case = next(
        item for item in load_vertical_slice_suite().cases if item.expected.outcome == "ready"
    )
    result, _ = asyncio.run(run_vertical_slice_case(case))
    payload = result.model_dump(mode="json")

    if mutation == "request_id":
        payload["request_id"] = "different-request"
    elif mutation == "data_mode":
        payload["data_mode"] = "live"
    elif mutation == "context_id":
        payload["planner"]["context_id"] = "planner-context-other"
    elif mutation == "plan_id":
        payload["validation"]["plan_id"] = "trip-plan-other"
    elif mutation == "outcome":
        payload["outcome"] = "conflicted"
    elif mutation == "final_status":
        payload["plan"]["status"] = "final"
    else:
        raise AssertionError(f"unknown mutation: {mutation}")

    with pytest.raises(ValidationError, match=expected_error):
        VerticalSliceResult.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("upstream_status", "requires candidates_ready"),
        ("request_id", "mismatched request ids"),
        ("context_id", "mismatched Planner context"),
        ("candidate_set", "complete provider candidate set"),
    ],
)
def test_assembler_rejects_broken_stage_inputs(
    mutation: str,
    expected_error: str,
) -> None:
    case = next(
        item for item in load_vertical_slice_suite().cases if item.expected.outcome == "ready"
    )
    result, _ = asyncio.run(run_vertical_slice_case(case))
    request = case.request
    upstream = result.upstream
    planner = result.planner

    if mutation == "upstream_status":
        upstream = upstream.model_copy(update={"status": PlanningWorkflowStatus.NO_CANDIDATE_QUERY})
    elif mutation == "request_id":
        request = request.model_copy(update={"request_id": "different-request"})
    elif mutation == "context_id":
        planner = planner.model_copy(update={"context_id": "planner-context-other"})
    elif mutation == "candidate_set":
        first_day = planner.day_plans[0]
        first_item = first_day.items[0].model_copy(update={"candidate_id": None})
        changed_day = first_day.model_copy(update={"items": (first_item,)})
        planner = planner.model_copy(update={"day_plans": (changed_day, *planner.day_plans[1:])})
    else:
        raise AssertionError(f"unknown mutation: {mutation}")

    with pytest.raises(VerticalSliceProtocolError, match=expected_error):
        assemble_trip_plan(request, upstream, planner, case.cost_items)


def test_vertical_slice_stops_before_planner_when_no_candidate_query_exists() -> None:
    case = load_vertical_slice_suite().cases[0]
    request = case.request.model_copy(update={"constraints": ConstraintSet()})
    provider = VerticalSliceScenarioProvider(case.provider_responses)
    model = FixturePlannerProposalModel(
        PlannerModelResponse(
            proposal=case.planner_proposal,
            model=case.planner_model,
            latency_ms=0,
        )
    )

    with pytest.raises(VerticalSliceProtocolError, match="no_candidate_query"):
        asyncio.run(
            run_trip_planning_vertical_slice(
                request,
                provider,
                model,
                case.cost_items,
                data_mode=DataMode.FIXTURE,
            )
        )
    assert provider.calls == []


def test_vertical_slice_result_rejects_non_ready_upstream_contract() -> None:
    case = next(
        item for item in load_vertical_slice_suite().cases if item.expected.outcome == "ready"
    )
    result, _ = asyncio.run(run_vertical_slice_case(case))
    stopped_upstream = result.upstream.model_copy(
        update={"status": PlanningWorkflowStatus.NO_CANDIDATE_QUERY}
    )
    broken_result = result.model_copy(update={"upstream": stopped_upstream})

    with pytest.raises(ValueError, match="requires a candidates_ready"):
        broken_result.validate_stage_lineage()
