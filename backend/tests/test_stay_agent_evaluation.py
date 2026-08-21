import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.agents import run_stay_agent
from app.agents.contracts import (
    ModelTokenUsage,
    StayCandidateSelectionProposal,
    StayEvidenceKind,
    StayEvidenceReference,
    StayQueryModelResponse,
    StayQueryProposal,
    StayQueryProposalBatch,
    StaySelectionModelResponse,
    StaySelectionProposalBatch,
)
from app.evaluation import (
    StayAgentBaselineReport,
    evaluate_stay_agent_case,
    evaluate_stay_agent_suite,
    load_stay_agent_suite,
    stay_agent_dataset_sha256,
)
from scripts.export_stay_agent_schemas import (
    DEFAULT_REPORT_SCHEMA_PATH,
    DEFAULT_SUITE_SCHEMA_PATH,
    build_stay_agent_report_schema,
    build_stay_agent_suite_schema,
)
from scripts.run_stay_agent_eval import DEFAULT_OUTPUT_PATH as LIVE_REPORT_PATH


class ExpectedStayModel:
    def __init__(self, expected_by_request_id: dict[str, Any]) -> None:
        self.expected_by_request_id = expected_by_request_id
        self.query_calls: list[str] = []
        self.selection_calls: list[str] = []

    def propose_queries(self, context: Any) -> StayQueryModelResponse:
        self.query_calls.append(context.request_id)
        expected = self.expected_by_request_id[context.request_id]
        return StayQueryModelResponse(
            proposal=StayQueryProposalBatch(
                items=(
                    StayQueryProposal(
                        target_area=f"{context.destination.normalized_name}偏好区域",
                        keywords=f"{context.destination.normalized_name}住宿",
                        reason="确定性评测模型覆盖人工标注的住宿区域依据。",
                        context_refs=expected.required_context_refs,
                    ),
                )
            ),
            model="fixture-stay-eval-model",
            latency_ms=10,
            usage=ModelTokenUsage(
                prompt_tokens=110,
                completion_tokens=20,
                total_tokens=130,
            ),
        )

    def select_candidates(
        self,
        context: Any,
        queries: Any,
        observations: Any,
    ) -> StaySelectionModelResponse:
        del queries
        self.selection_calls.append(context.request_id)
        expected = self.expected_by_request_id[context.request_id]
        observations_by_id = {item.candidate.candidate_id: item.candidate for item in observations}
        selected_ids: list[str] = []
        for group in expected.required_recommendation_groups:
            selected_id = group[0]
            if selected_id not in selected_ids:
                selected_ids.append(selected_id)
        proposals = tuple(
            StayCandidateSelectionProposal(
                candidate_id=candidate_id,
                rank=index + 1,
                reason="候选命中人工标注的住宿区域相关组。",
                evidence=(
                    StayEvidenceReference(
                        kind=StayEvidenceKind.AREA_NAME,
                        value=observations_by_id[candidate_id].area_name,
                    ),
                ),
            )
            for index, candidate_id in enumerate(selected_ids)
        )
        return StaySelectionModelResponse(
            proposal=StaySelectionProposalBatch(items=proposals),
            model="fixture-stay-eval-model",
            latency_ms=20,
            usage=ModelTokenUsage(
                prompt_tokens=150,
                completion_tokens=30,
                total_tokens=180,
            ),
        )


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def build_fixture_model() -> ExpectedStayModel:
    suite = load_stay_agent_suite()
    return ExpectedStayModel(
        {
            item.request.request_id: item.expected
            for item in suite.cases
            if item.expected.outcome == "recommendations"
        }
    )


def test_fixture_suite_proves_routing_grounding_relevance_and_truth_boundary() -> None:
    model = build_fixture_model()
    report = asyncio.run(
        evaluate_stay_agent_suite(
            lambda context, provider: run_stay_agent(context, provider, model),
            execution_mode="fixture",
            model="fixture-stay-eval-model",
        )
    )

    assert report.passed_case_count == 6
    assert report.model_call_count == 8
    assert report.provider_call_count == 4
    assert report.required_context_ref_count == report.matched_context_ref_count == 13
    assert report.recommendation_count == 4
    assert report.grounded_recommendation_count == 4
    assert report.traceable_recommendation_count == 4
    assert report.allowed_recommendation_count == 4
    assert report.required_recommendation_group_count == 4
    assert report.matched_recommendation_group_count == 4
    assert report.unverified_price_field_count == 0
    assert report.unknown_availability_count == 4
    assert report.booking_disabled_count == 4
    assert report.commercial_truth_boundary_passed is True
    assert str(report.case_pass_rate) == "1.0000"
    assert str(report.context_reference_coverage_rate) == "1.0000"
    assert str(report.grounding_rate) == "1.0000"
    assert str(report.source_traceability_rate) == "1.0000"
    assert str(report.labelled_relevance_rate) == "1.0000"
    assert str(report.recommendation_group_coverage_rate) == "1.0000"
    assert report.total_tokens == 1240
    assert report.p50_case_latency_ms == report.p95_case_latency_ms == 30
    assert len(model.query_calls) == len(model.selection_calls) == 4


@pytest.mark.parametrize(
    ("case_id", "expected_check"),
    [
        ("stay-blocked-missing-rooms-v1", "expected_clarification_present"),
        ("stay-blocked-unsupported-nanjing-v1", "expected_clarification_present"),
    ],
)
def test_blocked_cases_skip_runner_model_and_provider(
    case_id: str,
    expected_check: str,
) -> None:
    suite = load_stay_agent_suite()
    case = next(item for item in suite.cases if item.case_id == case_id)
    runner_calls: list[str] = []

    async def forbidden_runner(context: Any, provider: Any) -> Any:
        del provider
        runner_calls.append(context.request_id)
        raise AssertionError("blocked case must not enter the Stay runner")

    result = asyncio.run(evaluate_stay_agent_case(case, forbidden_runner))

    assert result.passed is True
    assert result.model_call_count == 0
    assert result.provider_call_count == 0
    assert result.recommendation_count == 0
    assert runner_calls == []
    assert next(item for item in result.checks if item.code == expected_check).passed is True


def test_report_uses_frozen_stay_suite_hash() -> None:
    suite = load_stay_agent_suite()
    model = build_fixture_model()
    report = asyncio.run(
        evaluate_stay_agent_suite(
            lambda context, provider: run_stay_agent(context, provider, model),
            execution_mode="fixture",
            model="fixture-stay-eval-model",
        )
    )

    assert report.dataset_sha256 == stay_agent_dataset_sha256(suite)


def test_stay_schemas_are_generated_and_valid() -> None:
    committed_suite = load_json(DEFAULT_SUITE_SCHEMA_PATH)
    committed_report = load_json(DEFAULT_REPORT_SCHEMA_PATH)

    assert committed_suite == build_stay_agent_suite_schema()
    assert committed_report == build_stay_agent_report_schema()
    Draft202012Validator.check_schema(committed_suite)
    Draft202012Validator.check_schema(committed_report)


def test_committed_live_report_matches_contract_and_frozen_dataset() -> None:
    report = StayAgentBaselineReport.model_validate(load_json(LIVE_REPORT_PATH))
    suite = load_stay_agent_suite()

    assert report.execution_mode == "live"
    assert report.model == "deepseek-v4-pro"
    assert report.dataset_sha256 == stay_agent_dataset_sha256(suite)
    assert report.passed_case_count == 6
    assert report.model_call_count == 8
    assert report.provider_call_count == 12
    assert report.required_context_ref_count == report.matched_context_ref_count == 13
    assert report.recommendation_count == report.grounded_recommendation_count == 8
    assert report.context_reference_coverage_rate == 1
    assert report.grounding_rate == report.source_traceability_rate == 1
    assert report.labelled_relevance_rate == 1
    assert report.recommendation_group_coverage_rate == 1
    assert report.unverified_price_field_count == 0
    assert report.commercial_truth_boundary_passed is True
    assert report.total_tokens == 12321
    assert report.p50_case_latency_ms == 7755
    assert report.p95_case_latency_ms == 8184


def test_evaluation_records_failure_without_fabricating_stays() -> None:
    suite = load_stay_agent_suite()
    case = next(item for item in suite.cases if item.expected.outcome == "recommendations")

    async def failed_runner(*_: Any) -> Any:
        raise RuntimeError("fixture model failed")

    result = asyncio.run(evaluate_stay_agent_case(case, failed_runner))

    assert result.passed is False
    assert result.recommendation_count == 0
    assert result.grounded_recommendation_count == 0
    assert result.error_code is not None
    assert result.checks[0].code == "stay_protocol_succeeded"
    assert result.checks[0].passed is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_call_count", 7),
        ("context_reference_coverage_rate", "0.5000"),
        ("commercial_truth_boundary_passed", False),
        ("p50_case_latency_ms", 999),
    ],
)
def test_report_contract_rejects_inconsistent_aggregates(
    field: str,
    value: object,
) -> None:
    model = build_fixture_model()
    report = asyncio.run(
        evaluate_stay_agent_suite(
            lambda context, provider: run_stay_agent(context, provider, model),
            execution_mode="fixture",
            model="fixture-stay-eval-model",
        )
    )
    payload = copy.deepcopy(report.model_dump(mode="json"))
    payload[field] = value

    with pytest.raises(ValidationError, match=field.split("_")[0]):
        StayAgentBaselineReport.model_validate(payload)
