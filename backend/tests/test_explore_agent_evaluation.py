import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.agents import run_explore_agent
from app.agents.contracts import (
    ExploreCandidateSelectionProposal,
    ExploreEvidenceKind,
    ExploreEvidenceReference,
    ExploreQueryKind,
    ExploreQueryModelResponse,
    ExploreQueryProposal,
    ExploreQueryProposalBatch,
    ExploreSelectionModelResponse,
    ExploreSelectionProposalBatch,
    ModelTokenUsage,
)
from app.evaluation import (
    ExploreAgentBaselineReport,
    evaluate_explore_agent_case,
    evaluate_explore_agent_suite,
    explore_agent_dataset_sha256,
    load_explore_agent_suite,
)
from scripts.export_explore_agent_schemas import (
    DEFAULT_REPORT_SCHEMA_PATH,
    DEFAULT_SUITE_SCHEMA_PATH,
    build_explore_agent_report_schema,
    build_explore_agent_suite_schema,
)
from scripts.run_explore_agent_eval import DEFAULT_OUTPUT_PATH as LIVE_REPORT_PATH


class ExpectedExploreModel:
    def __init__(self, expected_by_request_id: dict[str, Any]) -> None:
        self.expected_by_request_id = expected_by_request_id
        self.query_calls: list[str] = []
        self.selection_calls: list[str] = []

    def propose_queries(self, context: Any) -> ExploreQueryModelResponse:
        self.query_calls.append(context.request_id)
        expected = self.expected_by_request_id[context.request_id]
        queries = tuple(
            ExploreQueryProposal(
                kind=kind,
                keywords=(
                    f"{context.destination.normalized_name}本地美食餐厅"
                    if kind == ExploreQueryKind.DINING
                    else f"{context.destination.normalized_name}文化景点"
                ),
                reason="确定性评测模型覆盖人工标注的查询类型。",
                context_refs=(expected.required_context_refs if index == 0 else ()),
            )
            for index, kind in enumerate(expected.required_query_kinds)
        )
        return ExploreQueryModelResponse(
            proposal=ExploreQueryProposalBatch(items=queries),
            model="fixture-explore-eval-model",
            latency_ms=10,
            usage=ModelTokenUsage(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            ),
        )

    def select_candidates(
        self,
        context: Any,
        queries: Any,
        observations: Any,
    ) -> ExploreSelectionModelResponse:
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
            ExploreCandidateSelectionProposal(
                candidate_id=candidate_id,
                rank=index + 1,
                reason="候选命中人工标注的偏好相关组。",
                evidence=(
                    ExploreEvidenceReference(
                        kind=ExploreEvidenceKind.CATEGORY,
                        value=observations_by_id[candidate_id].categories[0],
                    ),
                ),
            )
            for index, candidate_id in enumerate(selected_ids)
        )
        return ExploreSelectionModelResponse(
            proposal=ExploreSelectionProposalBatch(items=proposals),
            model="fixture-explore-eval-model",
            latency_ms=20,
            usage=ModelTokenUsage(
                prompt_tokens=140,
                completion_tokens=30,
                total_tokens=170,
            ),
        )


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def build_fixture_model() -> ExpectedExploreModel:
    suite = load_explore_agent_suite()
    return ExpectedExploreModel({item.request.request_id: item.expected for item in suite.cases})


def test_fixture_suite_proves_query_coverage_grounding_and_labelled_relevance() -> None:
    model = build_fixture_model()
    report = asyncio.run(
        evaluate_explore_agent_suite(
            lambda context, provider: run_explore_agent(context, provider, model),
            execution_mode="fixture",
            model="fixture-explore-eval-model",
        )
    )

    assert report.passed_case_count == 6
    assert report.model_call_count == 12
    assert report.provider_call_count == 9
    assert report.required_query_kind_count == report.matched_query_kind_count == 9
    assert report.recommendation_count == 9
    assert report.grounded_recommendation_count == 9
    assert report.traceable_recommendation_count == 9
    assert report.allowed_recommendation_count == 9
    assert report.required_recommendation_group_count == 9
    assert report.matched_recommendation_group_count == 9
    assert str(report.case_pass_rate) == "1.0000"
    assert str(report.query_kind_coverage_rate) == "1.0000"
    assert str(report.grounding_rate) == "1.0000"
    assert str(report.source_traceability_rate) == "1.0000"
    assert str(report.labelled_relevance_rate) == "1.0000"
    assert str(report.recommendation_group_coverage_rate) == "1.0000"
    assert report.total_tokens == 1740
    assert report.p50_case_latency_ms == report.p95_case_latency_ms == 30
    assert len(model.query_calls) == len(model.selection_calls) == 6


def test_open_ended_case_runs_without_a_must_visit_constraint() -> None:
    suite = load_explore_agent_suite()
    case = next(item for item in suite.cases if item.case_id == "explore-beijing-open-history-v1")
    model = build_fixture_model()

    result = asyncio.run(
        evaluate_explore_agent_case(
            case,
            lambda context, provider: run_explore_agent(context, provider, model),
        )
    )

    assert result.passed is True
    assert result.query_count == 1
    assert model.query_calls == [case.request.request_id]


def test_report_uses_frozen_suite_hash() -> None:
    suite = load_explore_agent_suite()
    model = build_fixture_model()
    report = asyncio.run(
        evaluate_explore_agent_suite(
            lambda context, provider: run_explore_agent(context, provider, model),
            execution_mode="fixture",
            model="fixture-explore-eval-model",
        )
    )

    assert report.dataset_sha256 == explore_agent_dataset_sha256(suite)


def test_explore_schemas_are_generated_and_valid() -> None:
    committed_suite = load_json(DEFAULT_SUITE_SCHEMA_PATH)
    committed_report = load_json(DEFAULT_REPORT_SCHEMA_PATH)

    assert committed_suite == build_explore_agent_suite_schema()
    assert committed_report == build_explore_agent_report_schema()
    Draft202012Validator.check_schema(committed_suite)
    Draft202012Validator.check_schema(committed_report)


def test_committed_live_report_matches_contract_and_frozen_dataset() -> None:
    report = ExploreAgentBaselineReport.model_validate(load_json(LIVE_REPORT_PATH))
    suite = load_explore_agent_suite()

    assert report.execution_mode == "live"
    assert report.model == "deepseek-v4-pro"
    assert report.dataset_sha256 == explore_agent_dataset_sha256(suite)
    assert report.passed_case_count == 6
    assert report.model_call_count == 12
    assert report.required_query_kind_count == report.matched_query_kind_count == 9
    assert report.grounding_rate == report.source_traceability_rate == 1
    assert report.labelled_relevance_rate == 1
    assert report.recommendation_group_coverage_rate == 1
    assert report.total_tokens == 17679
    assert report.p50_case_latency_ms == 7408
    assert report.p95_case_latency_ms == 7742


def test_evaluation_records_failure_without_fabricating_recommendations() -> None:
    suite = load_explore_agent_suite()
    case = suite.cases[0]

    async def failed_runner(*_: Any) -> Any:
        raise RuntimeError("fixture model failed")

    result = asyncio.run(evaluate_explore_agent_case(case, failed_runner))

    assert result.passed is False
    assert result.recommendation_count == 0
    assert result.grounded_recommendation_count == 0
    assert result.error_code is not None
    assert result.checks[0].code == "explore_protocol_succeeded"
    assert result.checks[0].passed is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_call_count", 11),
        ("query_kind_coverage_rate", "0.5000"),
        ("labelled_relevance_rate", "0.5000"),
        ("p50_case_latency_ms", 999),
    ],
)
def test_report_contract_rejects_inconsistent_aggregates(
    field: str,
    value: object,
) -> None:
    model = build_fixture_model()
    report = asyncio.run(
        evaluate_explore_agent_suite(
            lambda context, provider: run_explore_agent(context, provider, model),
            execution_mode="fixture",
            model="fixture-explore-eval-model",
        )
    )
    payload = copy.deepcopy(report.model_dump(mode="json"))
    payload[field] = value

    with pytest.raises(ValidationError, match=field):
        ExploreAgentBaselineReport.model_validate(payload)
