import asyncio
import json
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.agents import stay_agent as stay_module
from app.agents.contracts import (
    ModelTokenUsage,
    StayAgentResult,
    StayCandidateObservation,
    StayCandidateSelectionProposal,
    StayEvidenceKind,
    StayEvidenceReference,
    StayQueryModelResponse,
    StayQueryProposal,
    StayQueryProposalBatch,
    StaySelectionModelResponse,
    StaySelectionProposalBatch,
)
from app.agents.stay_agent import (
    STAY_QUERY_TOOL_NAME,
    STAY_SELECTION_TOOL_NAME,
    DeepSeekStayProposalModel,
    StayAgentConfigurationError,
    StayAgentProtocolError,
    normalize_stay_queries,
    run_live_stay_agent,
    run_stay_agent,
)
from app.core.config import Settings
from app.domain.candidates import CandidateStay, StayPriceBasis
from app.domain.money import MoneyRange
from app.evaluation import load_planning_seed_suite
from app.planning import compile_planner_context
from app.providers.ports import StaySearchRequest


class FixedStayModel:
    def __init__(
        self,
        query_response: StayQueryModelResponse,
        *,
        bad_evidence: bool = False,
    ) -> None:
        self.query_response = query_response
        self.bad_evidence = bad_evidence
        self.query_calls: list[str] = []
        self.selection_calls: list[tuple[str, ...]] = []

    def propose_queries(self, context: Any) -> StayQueryModelResponse:
        self.query_calls.append(context.context_id)
        return self.query_response

    def select_candidates(
        self,
        context: Any,
        queries: Any,
        observations: Any,
    ) -> StaySelectionModelResponse:
        del context, queries
        self.selection_calls.append(tuple(item.candidate.candidate_id for item in observations))
        candidate = observations[0].candidate
        evidence_value = "不存在的区域" if self.bad_evidence else candidate.area_name
        return StaySelectionModelResponse(
            proposal=StaySelectionProposalBatch(
                items=(
                    StayCandidateSelectionProposal(
                        candidate_id=candidate.candidate_id,
                        rank=1,
                        reason="候选所在区域与行程搜索方向相符, 房价和库存仍需另行核验。",
                        evidence=(
                            StayEvidenceReference(
                                kind=StayEvidenceKind.AREA_NAME,
                                value=evidence_value,
                            ),
                        ),
                    ),
                )
            ),
            model="fixture-stay-model",
            latency_ms=21,
            usage=ModelTokenUsage(
                prompt_tokens=150,
                completion_tokens=32,
                total_tokens=182,
            ),
        )


class RecordingStayProvider:
    def __init__(self, responses: list[tuple[CandidateStay, ...]]) -> None:
        self.responses = responses
        self.calls: list[StaySearchRequest] = []

    async def search_stays(self, request: StaySearchRequest) -> tuple[CandidateStay, ...]:
        self.calls.append(request)
        return self.responses.pop(0)


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


def planning_context(case_id: str = "seed-standard-beijing-history-v1") -> Any:
    _, cases = load_planning_seed_suite()
    seed_case = next(item for item in cases if item.case_id == case_id)
    return compile_planner_context(seed_case.request)


def sample_candidate(*, name: str = "前门示例酒店") -> CandidateStay:
    _, cases = load_planning_seed_suite()
    seed_case = next(item for item in cases if item.case_id == "seed-standard-beijing-history-v1")
    poi = seed_case.provider.candidates[0]
    return CandidateStay(
        candidate_id="stay-hotel-001",
        name=name,
        city="北京市",
        district="东城区",
        address="前门示例路 1 号",
        location=poi.location,
        area_name="东城区",
        tags=("category:住宿服务", "area:历史文化"),
        source=poi.source,
    )


def make_query_response(
    *,
    context_ref: str = "travel_style:历史文化",
) -> StayQueryModelResponse:
    return StayQueryModelResponse(
        proposal=StayQueryProposalBatch(
            items=(
                StayQueryProposal(
                    target_area="东城区历史文化区",
                    keywords="北京东城区酒店",
                    reason="覆盖历史文化偏好并减少跨区移动。",
                    context_refs=(context_ref,),
                ),
            )
        ),
        model="fixture-stay-model",
        latency_ms=17,
        usage=ModelTokenUsage(prompt_tokens=110, completion_tokens=24, total_tokens=134),
    )


def tool_call_response(*, tool_name: str, arguments: str, call_count: int = 1) -> object:
    tool_calls = [
        SimpleNamespace(
            id=f"call_stay_{index}",
            type="function",
            function=SimpleNamespace(name=tool_name, arguments=arguments),
        )
        for index in range(call_count)
    ]
    message = SimpleNamespace(tool_calls=tool_calls, content=None)
    usage = SimpleNamespace(prompt_tokens=96, completion_tokens=23, total_tokens=119)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_graph_searches_provider_then_returns_grounded_stay_recommendation() -> None:
    context = planning_context()
    candidate = sample_candidate()
    model = FixedStayModel(make_query_response())
    provider = RecordingStayProvider([(candidate,)])

    result = asyncio.run(run_stay_agent(context, provider, model))

    assert model.query_calls == [context.context_id]
    assert model.selection_calls == [(candidate.candidate_id,)]
    assert len(provider.calls) == 1
    assert provider.calls[0].city_adcode == "110000"
    assert provider.calls[0].limit == 3
    assert result.recommendations[0].candidate == candidate
    assert result.observations[0].query_ids == (result.queries[0].query_id,)
    assert result.recommendations[0].candidate.nightly_price_estimate is None
    assert result.recommendations[0].candidate.availability_status == "unknown"
    assert result.recommendations[0].candidate.booking_supported is False


def test_duplicate_candidate_across_queries_is_deduplicated_with_lineage() -> None:
    context = planning_context()
    candidate = sample_candidate()
    response = make_query_response().model_copy(
        update={
            "proposal": StayQueryProposalBatch(
                items=(
                    make_query_response().proposal.items[0],
                    StayQueryProposal(
                        target_area="前门商圈",
                        keywords="北京前门住宿",
                        reason="补充相邻住宿区域搜索。",
                        context_refs=("travel_style:历史文化",),
                    ),
                )
            )
        }
    )
    result = asyncio.run(
        run_stay_agent(
            context,
            RecordingStayProvider([(candidate,), (candidate,)]),
            FixedStayModel(response),
        )
    )

    assert len(result.observations) == 1
    assert result.observations[0].query_ids == tuple(item.query_id for item in result.queries)


def test_blocked_stay_context_stops_before_model_or_provider_calls() -> None:
    context = planning_context("seed-hard-missing-rooms-lodging-v1")
    model = FixedStayModel(make_query_response())
    provider = RecordingStayProvider([(sample_candidate(),)])

    with pytest.raises(StayAgentProtocolError, match="blocked"):
        asyncio.run(run_stay_agent(context, provider, model))

    assert model.query_calls == []
    assert model.selection_calls == []
    assert provider.calls == []


def test_query_normalizer_rejects_untraceable_context_reference() -> None:
    with pytest.raises(StayAgentProtocolError, match="unknown context evidence"):
        normalize_stay_queries(
            planning_context(),
            make_query_response(context_ref="travel_style:用户从未表达的偏好"),
        )


def test_selection_rejects_fabricated_evidence() -> None:
    with pytest.raises(StayAgentProtocolError, match="not present"):
        asyncio.run(
            run_stay_agent(
                planning_context(),
                RecordingStayProvider([(sample_candidate(),)]),
                FixedStayModel(make_query_response(), bad_evidence=True),
            )
        )


def test_provider_cannot_reuse_candidate_id_for_different_stay_facts() -> None:
    candidate = sample_candidate()
    second = candidate.model_copy(update={"name": "被改写的同 ID 住宿"})
    response = make_query_response().model_copy(
        update={
            "proposal": StayQueryProposalBatch(
                items=(
                    make_query_response().proposal.items[0],
                    StayQueryProposal(
                        target_area="前门商圈",
                        keywords="北京前门住宿",
                        reason="补充相邻住宿区域搜索。",
                        context_refs=("travel_style:历史文化",),
                    ),
                )
            )
        }
    )

    with pytest.raises(StayAgentProtocolError, match="different candidate facts"):
        asyncio.run(
            run_stay_agent(
                planning_context(),
                RecordingStayProvider([(candidate,), (second,)]),
                FixedStayModel(response),
            )
        )


def test_result_contract_rejects_broken_stay_lineage() -> None:
    result = asyncio.run(
        run_stay_agent(
            planning_context(),
            RecordingStayProvider([(sample_candidate(),)]),
            FixedStayModel(make_query_response()),
        )
    )
    payload = result.model_dump(mode="json")
    payload["recommendations"][0]["candidate"]["name"] = "被改写的住宿名称"

    with pytest.raises(ValidationError):
        StayAgentResult.model_validate(payload)


def test_deepseek_adapter_uses_minimal_schemas_and_omits_price_from_selection() -> None:
    context = planning_context()
    candidate = sample_candidate().model_copy(
        update={
            "nightly_price_estimate": MoneyRange(minimum=Decimal("400"), maximum=Decimal("600")),
            "price_basis": StayPriceBasis.FIXTURE_ESTIMATE,
            "price_source": sample_candidate().source,
        }
    )
    query_proposal = make_query_response().proposal
    fake_client = FakeOpenAIClient(
        [
            tool_call_response(
                tool_name=STAY_QUERY_TOOL_NAME,
                arguments=query_proposal.model_dump_json(),
            )
        ]
    )
    model = DeepSeekStayProposalModel(make_settings())
    model._client = fake_client  # type: ignore[assignment]

    queries = normalize_stay_queries(context, model.propose_queries(context))
    observation = StayCandidateObservation(candidate=candidate, query_ids=(queries[0].query_id,))
    selection = StaySelectionProposalBatch(
        items=(
            StayCandidateSelectionProposal(
                candidate_id=candidate.candidate_id,
                rank=1,
                reason="区域与历史文化搜索方向相符。",
                evidence=(
                    StayEvidenceReference(
                        kind=StayEvidenceKind.AREA_NAME,
                        value=candidate.area_name,
                    ),
                ),
            ),
        )
    )
    fake_client.completions.responses.append(
        tool_call_response(
            tool_name=STAY_SELECTION_TOOL_NAME,
            arguments=selection.model_dump_json(),
        )
    )
    model.select_candidates(context, queries, (observation,))

    query_call, selection_call = fake_client.completions.calls
    assert query_call["tool_choice"]["function"]["name"] == STAY_QUERY_TOOL_NAME
    assert selection_call["tool_choice"]["function"]["name"] == STAY_SELECTION_TOOL_NAME
    query_schema = json.dumps(query_call["tools"][0]["function"]["parameters"])
    selection_schema = json.dumps(selection_call["tools"][0]["function"]["parameters"])
    selection_payload = str(selection_call["messages"][1]["content"])
    for forbidden in ("candidate_id", "source", "location", "nightly_price_estimate"):
        assert f'"{forbidden}"' not in query_schema
    for forbidden in ("name", "source", "location", "nightly_price_estimate"):
        assert f'"{forbidden}"' not in selection_schema
    assert "nightly_price_estimate" not in selection_payload
    assert '"400"' not in selection_payload
    assert '"600"' not in selection_payload


def test_deepseek_adapter_rejects_bad_tool_protocol() -> None:
    fake_client = FakeOpenAIClient(
        [
            tool_call_response(
                tool_name=STAY_QUERY_TOOL_NAME,
                arguments="{}",
                call_count=2,
            )
        ]
    )
    model = DeepSeekStayProposalModel(make_settings())
    model._client = fake_client  # type: ignore[assignment]

    with pytest.raises(StayAgentProtocolError, match="exactly one"):
        model.propose_queries(planning_context())


def test_live_runner_requires_tracing() -> None:
    with pytest.raises(StayAgentConfigurationError, match="LANGSMITH_TRACING"):
        asyncio.run(
            run_live_stay_agent(
                planning_context(),
                RecordingStayProvider([(sample_candidate(),)]),
                make_settings(tracing=False),
            )
        )


def test_live_runner_flushes_langsmith(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_langsmith = FakeLangSmithClient()
    model = FixedStayModel(make_query_response())
    monkeypatch.setattr(stay_module, "build_langsmith_client", lambda *_: fake_langsmith)
    monkeypatch.setattr(stay_module, "DeepSeekStayProposalModel", lambda *_: model)
    monkeypatch.setattr(stay_module, "tracing_context", lambda **_: nullcontext())

    result = asyncio.run(
        run_live_stay_agent(
            planning_context(),
            RecordingStayProvider([(sample_candidate(),)]),
            make_settings(),
        )
    )

    assert result.query_model == "fixture-stay-model"
    assert fake_langsmith.flush_timeouts == [15.0]
