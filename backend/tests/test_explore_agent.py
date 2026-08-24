import asyncio
import json
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.agents import explore_agent as explore_module
from app.agents.contracts import (
    ExploreAgentResult,
    ExploreCandidateObservation,
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
from app.agents.explore_agent import (
    EXPLORE_QUERY_TOOL_NAME,
    EXPLORE_SELECTION_TOOL_NAME,
    DeepSeekExploreProposalModel,
    ExploreAgentConfigurationError,
    ExploreAgentProtocolError,
    normalize_explore_queries,
    run_explore_agent,
    run_live_explore_agent,
)
from app.core.config import Settings
from app.domain.candidates import CandidatePOI
from app.domain.provider import ProviderErrorCategory, ProviderFailure
from app.evaluation import load_planning_seed_suite
from app.planning import compile_planner_context
from app.providers.errors import ProviderRequestError
from app.providers.ports import POISearchRequest, RouteRequest, WeatherRiskRequest


class FixedExploreModel:
    def __init__(
        self,
        query_response: ExploreQueryModelResponse,
        *,
        bad_evidence: bool = False,
    ) -> None:
        self.query_response = query_response
        self.bad_evidence = bad_evidence
        self.query_calls: list[str] = []
        self.selection_calls: list[tuple[str, ...]] = []

    def propose_queries(self, context: Any) -> ExploreQueryModelResponse:
        self.query_calls.append(context.context_id)
        return self.query_response

    def select_candidates(
        self,
        context: Any,
        queries: Any,
        observations: Any,
    ) -> ExploreSelectionModelResponse:
        del context, queries
        self.selection_calls.append(tuple(item.candidate.candidate_id for item in observations))
        candidate = observations[0].candidate
        evidence_value = "fabricated-category" if self.bad_evidence else candidate.categories[0]
        return ExploreSelectionModelResponse(
            proposal=ExploreSelectionProposalBatch(
                items=(
                    ExploreCandidateSelectionProposal(
                        candidate_id=candidate.candidate_id,
                        rank=1,
                        reason="候选类别与历史文化偏好相符, 后续仍需路线与营业时间校验。",
                        evidence=(
                            ExploreEvidenceReference(
                                kind=ExploreEvidenceKind.CATEGORY,
                                value=evidence_value,
                            ),
                        ),
                    ),
                )
            ),
            model="fixture-explore-model",
            latency_ms=22,
            usage=ModelTokenUsage(
                prompt_tokens=140,
                completion_tokens=30,
                total_tokens=170,
            ),
        )


class RecordingProvider:
    def __init__(self, responses: list[tuple[CandidatePOI, ...] | Exception]) -> None:
        self.responses = responses
        self.calls: list[POISearchRequest] = []

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        self.calls.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def get_weather_risks(self, request: WeatherRiskRequest) -> Any:
        raise AssertionError(f"unexpected weather request: {request}")

    async def get_route(self, request: RouteRequest) -> Any:
        raise AssertionError(f"unexpected route request: {request}")


class FakeCompletions:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeOpenAIClient:
    def __init__(self, responses: list[object]) -> None:
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeLangSmithClient:
    def __init__(self) -> None:
        self.flush_timeouts: list[float] = []

    def flush(self, timeout: float) -> None:
        self.flush_timeouts.append(timeout)


def make_settings(*, tracing: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("deepseek-test-secret-value"),
        langsmith_api_key=SecretStr("langsmith-test-secret-value"),
        langsmith_tracing=tracing,
    )


def sample_context_and_candidate() -> tuple[Any, CandidatePOI]:
    _, cases = load_planning_seed_suite()
    seed_case = next(item for item in cases if item.case_id == "seed-standard-beijing-history-v1")
    return compile_planner_context(seed_case.request), seed_case.provider.candidates[0]


def make_query_response(*, context_ref: str = "travel_style:历史文化") -> ExploreQueryModelResponse:
    return ExploreQueryModelResponse(
        proposal=ExploreQueryProposalBatch(
            items=(
                ExploreQueryProposal(
                    kind=ExploreQueryKind.ATTRACTION,
                    keywords="北京历史文化博物馆",
                    reason="覆盖历史文化偏好和已确认必去项。",
                    context_refs=(context_ref,),
                ),
            )
        ),
        model="fixture-explore-model",
        latency_ms=18,
        usage=ModelTokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )


def tool_call_response(*, tool_name: str, arguments: str, call_count: int = 1) -> object:
    tool_calls = [
        SimpleNamespace(
            id=f"call_explore_{index}",
            type="function",
            function=SimpleNamespace(name=tool_name, arguments=arguments),
        )
        for index in range(call_count)
    ]
    message = SimpleNamespace(tool_calls=tool_calls, content=None)
    usage = SimpleNamespace(prompt_tokens=90, completion_tokens=22, total_tokens=112)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_graph_searches_provider_then_returns_grounded_recommendation() -> None:
    context, candidate = sample_context_and_candidate()
    model = FixedExploreModel(make_query_response())
    provider = RecordingProvider([(candidate,)])

    result = asyncio.run(run_explore_agent(context, provider, model))

    assert model.query_calls == [context.context_id]
    assert model.selection_calls == [(candidate.candidate_id,)]
    assert len(provider.calls) == 1
    assert provider.calls[0].city_adcode == "110000"
    assert provider.calls[0].limit == 5
    assert result.recommendations[0].candidate == candidate
    assert result.recommendations[0].proposal.reason != candidate.name
    assert result.observations[0].query_ids == (result.queries[0].query_id,)
    assert result.query_usage is not None and result.query_usage.total_tokens == 120
    assert result.selection_usage is not None and result.selection_usage.total_tokens == 170


def test_duplicate_candidate_across_queries_is_deduplicated_with_lineage() -> None:
    context, candidate = sample_context_and_candidate()
    refreshed_candidate = candidate.model_copy(
        update={
            "source": candidate.source.model_copy(
                update={"retrieved_at": candidate.source.retrieved_at + timedelta(seconds=1)}
            )
        }
    )
    response = make_query_response().model_copy(
        update={
            "proposal": ExploreQueryProposalBatch(
                items=(
                    make_query_response().proposal.items[0],
                    ExploreQueryProposal(
                        kind=ExploreQueryKind.ATTRACTION,
                        keywords="北京世界遗产",
                        reason="补充世界遗产搜索视角。",
                        context_refs=("travel_style:历史文化",),
                    ),
                )
            )
        }
    )
    model = FixedExploreModel(response)
    provider = RecordingProvider([(candidate,), (refreshed_candidate,)])

    result = asyncio.run(run_explore_agent(context, provider, model))

    assert len(result.observations) == 1
    assert result.observations[0].query_ids == tuple(item.query_id for item in result.queries)


def test_query_level_detail_failure_preserves_other_grounded_candidates() -> None:
    context, candidate = sample_context_and_candidate()
    response = make_query_response().model_copy(
        update={
            "proposal": ExploreQueryProposalBatch(
                items=(
                    make_query_response().proposal.items[0],
                    ExploreQueryProposal(
                        kind=ExploreQueryKind.DINING,
                        keywords="北京特色小吃",
                        reason="补充当地餐饮候选。",
                        context_refs=(),
                    ),
                )
            )
        }
    )
    detail_failure = ProviderRequestError(
        ProviderFailure(
            provider="amap",
            operation="maps_search_detail",
            category=ProviderErrorCategory.UNRECOVERABLE,
            message="maps_search_detail returned invalid JSON content",
            retryable=False,
        )
    )
    provider = RecordingProvider([(candidate,), detail_failure])

    result = asyncio.run(run_explore_agent(context, provider, FixedExploreModel(response)))

    assert len(provider.calls) == 2
    assert [item.candidate.candidate_id for item in result.observations] == [candidate.candidate_id]


@pytest.mark.parametrize("corruption", ["unknown_query", "changed_candidate"])
def test_result_contract_rejects_broken_observation_lineage(corruption: str) -> None:
    context, candidate = sample_context_and_candidate()
    result = asyncio.run(
        run_explore_agent(
            context,
            RecordingProvider([(candidate,)]),
            FixedExploreModel(make_query_response()),
        )
    )
    payload = result.model_dump(mode="json")
    if corruption == "unknown_query":
        payload["observations"][0]["query_ids"] = ["explore-query-not-returned"]
    else:
        payload["recommendations"][0]["candidate"]["name"] = "被改写的候选名称"

    with pytest.raises(ValidationError):
        ExploreAgentResult.model_validate(payload)


def test_query_normalizer_rejects_untraceable_context_reference() -> None:
    context, _ = sample_context_and_candidate()

    with pytest.raises(ExploreAgentProtocolError, match="unknown context evidence"):
        normalize_explore_queries(
            context,
            make_query_response(context_ref="travel_style:用户从未表达的偏好"),
        )


def test_selection_rejects_fabricated_evidence() -> None:
    context, candidate = sample_context_and_candidate()
    model = FixedExploreModel(make_query_response(), bad_evidence=True)
    provider = RecordingProvider([(candidate,)])

    with pytest.raises(ExploreAgentProtocolError, match="not present"):
        asyncio.run(run_explore_agent(context, provider, model))


def test_provider_cannot_reuse_candidate_id_for_different_facts() -> None:
    context, candidate = sample_context_and_candidate()
    second = candidate.model_copy(update={"name": "伪造的同 ID 景点"})
    response = make_query_response().model_copy(
        update={
            "proposal": ExploreQueryProposalBatch(
                items=(
                    make_query_response().proposal.items[0],
                    ExploreQueryProposal(
                        kind=ExploreQueryKind.ATTRACTION,
                        keywords="北京古迹",
                        reason="补充古迹搜索视角。",
                        context_refs=("travel_style:历史文化",),
                    ),
                )
            )
        }
    )

    with pytest.raises(ExploreAgentProtocolError, match="different candidate facts"):
        asyncio.run(
            run_explore_agent(
                context,
                RecordingProvider([(candidate,), (second,)]),
                FixedExploreModel(response),
            )
        )


def test_deepseek_adapter_forces_separate_minimal_schemas() -> None:
    context, candidate = sample_context_and_candidate()
    query_proposal = make_query_response().proposal
    query_response = tool_call_response(
        tool_name=EXPLORE_QUERY_TOOL_NAME,
        arguments=query_proposal.model_dump_json(),
    )
    fake_client = FakeOpenAIClient([query_response])
    model = DeepSeekExploreProposalModel(make_settings())
    model._client = fake_client  # type: ignore[assignment]

    normalized_queries = normalize_explore_queries(context, model.propose_queries(context))
    observation = ExploreCandidateObservation(
        candidate=candidate,
        query_ids=(normalized_queries[0].query_id,),
    )
    selection = ExploreSelectionProposalBatch(
        items=(
            ExploreCandidateSelectionProposal(
                candidate_id=candidate.candidate_id,
                rank=1,
                reason="类别与偏好相符。",
                evidence=(
                    ExploreEvidenceReference(
                        kind=ExploreEvidenceKind.CATEGORY,
                        value=candidate.categories[0],
                    ),
                ),
            ),
        )
    )
    fake_client.completions.responses.append(
        tool_call_response(
            tool_name=EXPLORE_SELECTION_TOOL_NAME,
            arguments=selection.model_dump_json(),
        )
    )
    model.select_candidates(context, normalized_queries, (observation,))

    assert len(fake_client.completions.calls) == 2
    query_call, selection_call = fake_client.completions.calls
    assert query_call["tool_choice"]["function"]["name"] == EXPLORE_QUERY_TOOL_NAME
    assert selection_call["tool_choice"]["function"]["name"] == EXPLORE_SELECTION_TOOL_NAME
    serialized_query_schema = json.dumps(query_call["tools"][0]["function"]["parameters"])
    serialized_selection_schema = json.dumps(selection_call["tools"][0]["function"]["parameters"])
    for forbidden in ("candidate_id", "source", "location"):
        assert forbidden not in serialized_query_schema
    for forbidden in ("name", "source", "location", "categories"):
        assert forbidden not in serialized_selection_schema


def test_deepseek_adapter_rejects_bad_tool_protocol() -> None:
    context, _ = sample_context_and_candidate()
    fake_client = FakeOpenAIClient(
        [
            tool_call_response(
                tool_name=EXPLORE_QUERY_TOOL_NAME,
                arguments="{}",
                call_count=2,
            )
        ]
    )
    model = DeepSeekExploreProposalModel(make_settings())
    model._client = fake_client  # type: ignore[assignment]

    with pytest.raises(ExploreAgentProtocolError, match="exactly one"):
        model.propose_queries(context)


def test_live_runner_requires_tracing() -> None:
    context, candidate = sample_context_and_candidate()

    with pytest.raises(ExploreAgentConfigurationError, match="LANGSMITH_TRACING"):
        asyncio.run(
            run_live_explore_agent(
                context,
                RecordingProvider([(candidate,)]),
                make_settings(tracing=False),
            )
        )


def test_live_runner_flushes_langsmith(monkeypatch: pytest.MonkeyPatch) -> None:
    context, candidate = sample_context_and_candidate()
    fake_langsmith = FakeLangSmithClient()
    model = FixedExploreModel(make_query_response())

    monkeypatch.setattr(explore_module, "build_langsmith_client", lambda *_: fake_langsmith)
    monkeypatch.setattr(explore_module, "DeepSeekExploreProposalModel", lambda *_: model)
    monkeypatch.setattr(explore_module, "tracing_context", lambda **_: nullcontext())

    result = asyncio.run(
        run_live_explore_agent(
            context,
            RecordingProvider([(candidate,)]),
            make_settings(),
        )
    )

    assert result.query_model == "fixture-explore-model"
    assert fake_langsmith.flush_timeouts == [15.0]
