import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from app.agents import single_planner as planner_module
from app.agents.contracts import (
    ModelTokenUsage,
    PlannerModelResponse,
    PlannerPlacementProposal,
    PlannerProposalBatch,
)
from app.agents.single_planner import (
    PLANNER_PROPOSAL_TOOL_NAME,
    DeepSeekPlannerProposalModel,
    SinglePlannerConfigurationError,
    SinglePlannerProtocolError,
    candidate_set_sha256,
    normalize_planner_response,
    run_live_single_planner,
    run_single_planner,
)
from app.core.config import Settings
from app.domain.sources import DataMode
from app.evaluation.planning_seed import ScenarioTravelDataProvider, load_planning_seed_suite
from app.planning import run_minimal_planning_graph


class FixedPlannerModel:
    def __init__(self, response: PlannerModelResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def propose(self, context: Any, candidates: Any) -> PlannerModelResponse:
        self.calls.append((context.context_id, tuple(item.candidate_id for item in candidates)))
        return self.response


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


def candidate_ready_input() -> tuple[Any, tuple[Any, ...]]:
    _, cases = load_planning_seed_suite()
    seed_case = next(item for item in cases if item.expected.status == "candidates_ready")
    provider = ScenarioTravelDataProvider(seed_case.provider)
    result = asyncio.run(
        run_minimal_planning_graph(
            seed_case.request,
            provider,
            data_mode=DataMode.FIXTURE,
        )
    )
    provider.verify_complete()
    return result.planner_context, result.candidates


def make_response(
    candidate_id: str,
    *,
    day_number: int = 1,
    start_time: str = "09:00",
    model: str = "fixture-planner-model",
) -> PlannerModelResponse:
    return PlannerModelResponse(
        proposal=PlannerProposalBatch(
            items=(
                PlannerPlacementProposal(
                    candidate_id=candidate_id,
                    day_number=day_number,
                    start_time=start_time,
                    reason="把必去景点安排在上午, 后续补路线和营业时间校验。",
                ),
            )
        ),
        model=model,
        latency_ms=18,
        usage=ModelTokenUsage(prompt_tokens=120, completion_tokens=30, total_tokens=150),
    )


def tool_call_response(*, arguments: str, call_count: int = 1) -> object:
    tool_calls = [
        SimpleNamespace(
            id=f"call_planner_{index}",
            type="function",
            function=SimpleNamespace(name=PLANNER_PROPOSAL_TOOL_NAME, arguments=arguments),
        )
        for index in range(call_count)
    ]
    message = SimpleNamespace(tool_calls=tool_calls, content=None)
    usage = SimpleNamespace(prompt_tokens=90, completion_tokens=22, total_tokens=112)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_normalizer_copies_candidate_facts_and_builds_partial_day_plan() -> None:
    context, candidates = candidate_ready_input()
    candidate = candidates[0]

    result = normalize_planner_response(context, candidates, make_response(candidate.candidate_id))

    assert result.input_candidates_sha256 == candidate_set_sha256(candidates)
    assert len(result.day_plans) == 1
    item = result.day_plans[0].items[0]
    assert item.candidate_id == candidate.candidate_id
    assert item.title == candidate.name
    assert item.source == candidate.source
    assert result.decisions[0].proposal.reason not in item.notes
    assert item.start_at.date() == context.days[0].date
    assert (item.end_at - item.start_at).total_seconds() == 120 * 60
    assert result.day_plans[0].date == context.days[0].date


def test_graph_calls_model_once_and_returns_stable_result() -> None:
    context, candidates = candidate_ready_input()
    model = FixedPlannerModel(make_response(candidates[0].candidate_id))

    first = run_single_planner(context, candidates, model)
    second = run_single_planner(context, candidates, model)

    assert first == second
    assert model.calls == [
        (context.context_id, (candidates[0].candidate_id,)),
        (context.context_id, (candidates[0].candidate_id,)),
    ]


@pytest.mark.parametrize("failure", ["unknown", "duplicate", "omitted", "outside_day"])
def test_normalizer_rejects_ungrounded_or_invalid_placement(failure: str) -> None:
    context, candidates = candidate_ready_input()
    candidate_id = candidates[0].candidate_id
    if failure == "unknown":
        response = make_response("candidate-not-returned")
    elif failure == "duplicate":
        item = make_response(candidate_id).proposal.items[0]
        response = PlannerModelResponse(
            proposal=PlannerProposalBatch(items=(item, item)),
            model="fixture-planner-model",
            latency_ms=1,
        )
    elif failure == "omitted":
        extra = candidates[0].model_copy(
            update={"candidate_id": "candidate-second", "name": "第二个候选"}
        )
        candidates = (*candidates, extra)
        response = make_response(candidate_id)
    elif failure == "outside_day":
        response = make_response(candidate_id, day_number=5)
    else:
        raise AssertionError(f"unknown failure: {failure}")

    with pytest.raises(SinglePlannerProtocolError):
        normalize_planner_response(context, candidates, response)


def test_normalizer_rejects_overlapping_items() -> None:
    context, candidates = candidate_ready_input()
    extra = candidates[0].model_copy(
        update={"candidate_id": "candidate-second", "name": "第二个候选"}
    )
    first = make_response(candidates[0].candidate_id).proposal.items[0]
    second = make_response(extra.candidate_id).proposal.items[0]
    response = PlannerModelResponse(
        proposal=PlannerProposalBatch(items=(first, second)),
        model="fixture-planner-model",
        latency_ms=1,
    )

    with pytest.raises(SinglePlannerProtocolError, match="invalid timeline"):
        normalize_planner_response(context, (*candidates, extra), response)


def test_deepseek_adapter_forces_schema_tool_and_excludes_candidate_facts() -> None:
    context, candidates = candidate_ready_input()
    proposal = make_response(candidates[0].candidate_id).proposal
    fake_client = FakeOpenAIClient([tool_call_response(arguments=proposal.model_dump_json())])
    model = DeepSeekPlannerProposalModel(make_settings())
    model._client = fake_client  # type: ignore[assignment]

    response = model.propose(context, candidates)

    assert response.proposal == proposal
    assert response.usage is not None and response.usage.total_tokens == 112
    call = fake_client.completions.calls[0]
    assert call["tool_choice"]["function"]["name"] == PLANNER_PROPOSAL_TOOL_NAME
    schema = call["tools"][0]["function"]["parameters"]
    placement_schema = schema["$defs"]["PlannerPlacementProposal"]
    assert set(placement_schema["properties"]) == {
        "candidate_id",
        "day_number",
        "start_time",
        "reason",
    }
    serialized_schema = json.dumps(placement_schema["properties"])
    for forbidden in ("source", "provider", "end_at", "item_id"):
        assert forbidden not in serialized_schema


def test_deepseek_adapter_rejects_bad_tool_protocol() -> None:
    context, candidates = candidate_ready_input()
    fake_client = FakeOpenAIClient([tool_call_response(arguments="{}", call_count=2)])
    model = DeepSeekPlannerProposalModel(make_settings())
    model._client = fake_client  # type: ignore[assignment]

    with pytest.raises(SinglePlannerProtocolError, match="exactly one"):
        model.propose(context, candidates)


def test_live_runner_requires_tracing() -> None:
    context, candidates = candidate_ready_input()

    with pytest.raises(SinglePlannerConfigurationError, match="LANGSMITH_TRACING"):
        run_live_single_planner(context, candidates, make_settings(tracing=False))


def test_live_runner_flushes_langsmith(monkeypatch: pytest.MonkeyPatch) -> None:
    context, candidates = candidate_ready_input()
    fake_langsmith = FakeLangSmithClient()
    model = FixedPlannerModel(make_response(candidates[0].candidate_id))

    monkeypatch.setattr(planner_module, "build_langsmith_client", lambda *_: fake_langsmith)
    monkeypatch.setattr(planner_module, "DeepSeekPlannerProposalModel", lambda *_: model)
    monkeypatch.setattr(planner_module, "tracing_context", lambda **_: nullcontext())

    result = run_live_single_planner(context, candidates, make_settings())

    assert result.model == "fixture-planner-model"
    assert fake_langsmith.flush_timeouts == [15.0]
