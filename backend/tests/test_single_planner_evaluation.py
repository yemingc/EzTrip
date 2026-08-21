import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.agents import run_single_planner
from app.agents.contracts import (
    ModelTokenUsage,
    PlannerModelResponse,
    PlannerPlacementProposal,
    PlannerProposalBatch,
)
from app.evaluation import (
    SinglePlannerBaselineReport,
    evaluate_single_planner_suite,
    load_planning_seed_suite,
)
from app.evaluation.planning_seed import planning_seed_dataset_sha256
from app.evaluation.single_planner import evaluate_single_planner_case
from scripts.export_single_planner_schema import (
    DEFAULT_OUTPUT_PATH as SINGLE_PLANNER_SCHEMA_PATH,
)
from scripts.export_single_planner_schema import build_single_planner_report_schema
from scripts.run_single_planner_eval import DEFAULT_OUTPUT_PATH as LIVE_REPORT_PATH


class DeterministicPlannerModel:
    def __init__(self) -> None:
        self.call_count = 0

    def propose(self, context: Any, candidates: Any) -> PlannerModelResponse:
        self.call_count += 1
        return PlannerModelResponse(
            proposal=PlannerProposalBatch(
                items=tuple(
                    PlannerPlacementProposal(
                        candidate_id=item.candidate_id,
                        day_number=1,
                        start_time=f"{9 + index * 3:02d}:00",
                        reason="确定性评测模型只验证 grounding 和工作流路由。",
                    )
                    for index, item in enumerate(candidates)
                )
            ),
            model="fixture-single-planner",
            latency_ms=20 + self.call_count,
            usage=ModelTokenUsage(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            ),
        )


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_fixture_suite_routes_only_eligible_cases_and_preserves_grounding() -> None:
    model = DeterministicPlannerModel()
    report = asyncio.run(
        evaluate_single_planner_suite(
            lambda context, candidates: run_single_planner(context, candidates, model),
            execution_mode="fixture",
            model="fixture-single-planner",
        )
    )

    assert report.passed_case_count == 10
    assert report.planning_expected_case_count == 6
    assert report.model_call_count == 6
    assert model.call_count == 6
    assert report.planned_case_count == 6
    assert report.skipped_case_count == 4
    assert report.failed_case_count == 0
    assert report.candidate_count == 6
    assert report.scheduled_candidate_count == 6
    assert str(report.candidate_coverage_rate) == "1.0000"
    assert str(report.grounding_rate) == "1.0000"
    assert str(report.source_traceability_rate) == "1.0000"
    assert report.total_tokens == 720
    assert report.p50_latency_ms == 23
    assert report.p95_latency_ms == 26


def test_report_dataset_hash_is_the_planning_seed_hash() -> None:
    _, cases = load_planning_seed_suite()
    model = DeterministicPlannerModel()
    report = asyncio.run(
        evaluate_single_planner_suite(
            lambda context, candidates: run_single_planner(context, candidates, model),
            execution_mode="fixture",
            model="fixture-single-planner",
        )
    )

    assert report.dataset_sha256 == planning_seed_dataset_sha256(cases)


def test_single_planner_report_schema_is_generated_and_valid() -> None:
    committed = load_json(SINGLE_PLANNER_SCHEMA_PATH)
    generated = build_single_planner_report_schema()

    assert committed == generated
    Draft202012Validator.check_schema(committed)


def test_committed_live_report_matches_contract_and_frozen_dataset() -> None:
    payload = load_json(LIVE_REPORT_PATH)
    report = SinglePlannerBaselineReport.model_validate(payload)
    _, cases = load_planning_seed_suite()

    assert report.execution_mode == "live"
    assert report.model == "deepseek-v4-pro"
    assert report.dataset_sha256 == planning_seed_dataset_sha256(cases)
    assert report.passed_case_count == 10
    assert report.model_call_count == 6
    assert report.candidate_count == report.scheduled_candidate_count == 6
    assert report.total_tokens == 6235
    assert report.p50_latency_ms == 2797
    assert report.p95_latency_ms == 3968


def test_evaluation_records_protocol_failure_without_fabricating_schedule() -> None:
    _, cases = load_planning_seed_suite()
    eligible = next(item for item in cases if item.expected.status == "candidates_ready")

    def invalid_runner(context: Any, candidates: Any) -> Any:
        model = DeterministicPlannerModel()
        response = model.propose(context, candidates)
        payload = response.model_dump(mode="json")
        payload["proposal"]["items"][0]["candidate_id"] = "candidate-not-returned"
        bad_response = PlannerModelResponse.model_validate(payload)

        class BadModel:
            def propose(self, *_: Any) -> PlannerModelResponse:
                return bad_response

        return run_single_planner(context, candidates, BadModel())

    result = asyncio.run(evaluate_single_planner_case(eligible, invalid_runner))

    assert result.passed is False
    assert result.outcome == "failed"
    assert result.model_called is True
    assert result.scheduled_candidate_count == 0
    assert result.error_code == "singleplannerprotocol-error"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_call_count", 5),
        ("candidate_coverage_rate", "0.5000"),
        ("grounding_rate", "0.5000"),
        ("p50_latency_ms", 999),
    ],
)
def test_report_contract_rejects_inconsistent_aggregates(field: str, value: object) -> None:
    model = DeterministicPlannerModel()
    report = asyncio.run(
        evaluate_single_planner_suite(
            lambda context, candidates: run_single_planner(context, candidates, model),
            execution_mode="fixture",
            model="fixture-single-planner",
        )
    )
    payload = copy.deepcopy(report.model_dump(mode="json"))
    payload[field] = value

    with pytest.raises(ValidationError, match=field):
        SinglePlannerBaselineReport.model_validate(payload)
