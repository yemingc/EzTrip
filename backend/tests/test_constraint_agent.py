import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from app.agents import constraint_agent as agent_module
from app.agents.constraint_agent import (
    CONSTRAINT_PROPOSAL_TOOL_NAME,
    ConstraintAgentConfigurationError,
    ConstraintAgentProtocolError,
    DeepSeekConstraintProposalModel,
    normalize_constraint_response,
    replace_trip_request_constraints,
    run_constraint_agent,
    run_live_constraint_agent,
)
from app.agents.contracts import (
    ConstraintEvidenceMode,
    ConstraintModelResponse,
    ConstraintProposalBatch,
    ConstraintProposalItem,
    ModelTokenUsage,
)
from app.core.config import Settings
from app.domain.request import ConstraintKind, ConstraintSource, ConstraintStrength
from app.evaluation import load_planning_seed_suite
from app.planning import compile_planner_context


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


class FixedProposalModel:
    def __init__(self, response: ConstraintModelResponse) -> None:
        self.response = response
        self.requests: list[str] = []

    def propose(self, raw_text: str) -> ConstraintModelResponse:
        self.requests.append(raw_text)
        return self.response


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


def make_response(
    *items: ConstraintProposalItem,
    model: str = "fixture-constraint-model",
) -> ConstraintModelResponse:
    return ConstraintModelResponse(
        proposal=ConstraintProposalBatch(items=items),
        model=model,
        latency_ms=12,
        usage=ModelTokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )


def tool_call_response(*, arguments: str, call_count: int = 1) -> object:
    tool_calls = [
        SimpleNamespace(
            id=f"call_constraint_{index}",
            type="function",
            function=SimpleNamespace(
                name=CONSTRAINT_PROPOSAL_TOOL_NAME,
                arguments=arguments,
            ),
        )
        for index in range(call_count)
    ]
    message = SimpleNamespace(tool_calls=tool_calls, content=None)
    usage = SimpleNamespace(prompt_tokens=80, completion_tokens=15, total_tokens=95)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def first_seed_request() -> object:
    _, cases = load_planning_seed_suite()
    return cases[0].request


def test_normalizer_derives_confirmation_source_ids_and_hitl() -> None:
    request = first_seed_request()
    response = make_response(
        ConstraintProposalItem(
            kind="must_visit",
            value="故宫",
            strength="hard",
            priority=5,
            evidence="必须去故宫",
            evidence_mode="explicit",
        ),
        ConstraintProposalItem(
            kind="walking_intensity",
            value="轻步行",
            strength="soft",
            priority=4,
            evidence="轻步行",
            evidence_mode="inferred",
        ),
    )

    first = normalize_constraint_response(request.request_id, request.raw_text, response)
    second = normalize_constraint_response(request.request_id, request.raw_text, response)

    assert first == second
    assert first.raw_text_sha256 != request.raw_text
    assert tuple(item.constraint.value for item in first.decisions) == ("故宫", "low")
    explicit, inferred = first.constraints.items
    assert explicit.source == ConstraintSource.USER_EXPLICIT
    assert explicit.confirmed is True
    assert inferred.source == ConstraintSource.AGENT_INFERRED
    assert inferred.confirmed is False
    assert first.hitl_constraint_ids == (inferred.constraint_id,)
    assert not explicit.constraint_id.startswith("must_visit_forbidden_city")


def test_graph_result_can_replace_only_the_constraint_slice() -> None:
    request = first_seed_request()
    response = make_response(
        ConstraintProposalItem(
            kind="must_visit",
            value="故宫",
            strength="hard",
            priority=5,
            evidence="必须去故宫",
            evidence_mode="explicit",
        )
    )
    model = FixedProposalModel(response)

    result = run_constraint_agent(request, model)
    enriched = replace_trip_request_constraints(request, result)
    context = compile_planner_context(enriched)

    assert model.requests == [request.raw_text]
    assert enriched.destination_city == request.destination_city
    assert enriched.budget == request.budget
    assert len(context.confirmed_hard_constraints) == 1
    assert context.pending_constraints == ()


@pytest.mark.parametrize(
    ("evidence", "value"),
    [("原文不存在的证据", "故宫"), ("轻步行", "每天暴走四万步")],
)
def test_normalizer_rejects_unsupported_evidence_or_walking_value(
    evidence: str,
    value: str,
) -> None:
    request = first_seed_request()
    response = make_response(
        ConstraintProposalItem(
            kind="walking_intensity",
            value=value,
            strength="soft",
            priority=3,
            evidence=evidence,
            evidence_mode="explicit",
        )
    )

    with pytest.raises(ConstraintAgentProtocolError):
        normalize_constraint_response(request.request_id, request.raw_text, response)


def test_normalizer_rejects_duplicate_semantic_constraints() -> None:
    request = first_seed_request()
    response = make_response(
        ConstraintProposalItem(
            kind="must_visit",
            value="故宫",
            strength="hard",
            priority=5,
            evidence="必须去故宫",
            evidence_mode="explicit",
        ),
        ConstraintProposalItem(
            kind="must_visit",
            value="故宫",
            strength="soft",
            priority=2,
            evidence="故宫",
            evidence_mode="explicit",
        ),
    )

    with pytest.raises(ConstraintAgentProtocolError, match="duplicate"):
        normalize_constraint_response(request.request_id, request.raw_text, response)


def test_normalizer_rejects_conflicting_hard_constraint_set() -> None:
    request = first_seed_request()
    response = make_response(
        ConstraintProposalItem(
            kind="must_visit",
            value="故宫",
            strength="hard",
            priority=5,
            evidence="故宫",
            evidence_mode="explicit",
        ),
        ConstraintProposalItem(
            kind="avoid",
            value="故宫",
            strength="hard",
            priority=5,
            evidence="故宫",
            evidence_mode="explicit",
        ),
    )

    with pytest.raises(ConstraintAgentProtocolError, match="constraint set"):
        normalize_constraint_response(request.request_id, request.raw_text, response)


def test_deepseek_adapter_forces_schema_tool_and_records_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = json.dumps(
        {
            "items": [
                {
                    "kind": "must_visit",
                    "value": "故宫",
                    "strength": "hard",
                    "priority": 5,
                    "evidence": "必须去故宫",
                    "evidence_mode": "explicit",
                }
            ]
        },
        ensure_ascii=False,
    )
    fake_client = FakeOpenAIClient([tool_call_response(arguments=arguments)])
    constructor_arguments: dict[str, object] = {}

    def fake_openai(**kwargs: object) -> FakeOpenAIClient:
        constructor_arguments.update(kwargs)
        return fake_client

    monkeypatch.setattr(agent_module, "OpenAI", fake_openai)
    monkeypatch.setattr(agent_module, "wrap_openai", lambda client, **_: client)

    model = DeepSeekConstraintProposalModel(make_settings())
    response = model.propose("必须去故宫")

    assert constructor_arguments["base_url"] == "https://api.deepseek.com"
    assert response.model == "deepseek-v4-pro"
    assert response.usage is not None and response.usage.total_tokens == 95
    call = fake_client.completions.calls[0]
    assert call["tool_choice"] == {
        "type": "function",
        "function": {"name": CONSTRAINT_PROPOSAL_TOOL_NAME},
    }
    parameters = json.dumps(call["tools"][0]["function"]["parameters"])
    assert "confirmed" not in parameters
    assert "constraint_id" not in parameters
    assert "source" not in parameters


@pytest.mark.parametrize(
    ("arguments", "call_count"),
    [("not-json", 1), ('{"items": []}', 2)],
)
def test_deepseek_adapter_rejects_malformed_or_multiple_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
    arguments: str,
    call_count: int,
) -> None:
    fake_client = FakeOpenAIClient([tool_call_response(arguments=arguments, call_count=call_count)])
    monkeypatch.setattr(agent_module, "OpenAI", lambda **_: fake_client)
    monkeypatch.setattr(agent_module, "wrap_openai", lambda client, **_: client)
    model = DeepSeekConstraintProposalModel(make_settings())

    with pytest.raises(ConstraintAgentProtocolError):
        model.propose("必须去故宫")


def test_inferred_hard_constraint_stays_pending_for_planner_hitl() -> None:
    _, cases = load_planning_seed_suite()
    request = next(case.request for case in cases if "unconfirmed" in case.case_id)
    result = run_constraint_agent(
        request,
        FixedProposalModel(
            make_response(
                ConstraintProposalItem(
                    kind=ConstraintKind.MUST_VISIT,
                    value="故宫",
                    strength=ConstraintStrength.HARD,
                    priority=4,
                    evidence="还没决定要不要把故宫列为必去",
                    evidence_mode=ConstraintEvidenceMode.INFERRED,
                )
            )
        ),
    )
    enriched = replace_trip_request_constraints(request, result)
    context = compile_planner_context(enriched)

    assert len(context.pending_constraints) == 1
    assert context.confirmed_hard_constraints == ()
    assert context.clarifications[0].constraint_id == result.hitl_constraint_ids[0]


def test_uncertainty_marker_cannot_be_promoted_to_explicit_confirmation() -> None:
    _, cases = load_planning_seed_suite()
    request = next(case.request for case in cases if "unconfirmed" in case.case_id)
    response = make_response(
        ConstraintProposalItem(
            kind="must_visit",
            value="故宫",
            strength="hard",
            priority=5,
            evidence="故宫",
            evidence_mode="explicit",
        )
    )

    with pytest.raises(ConstraintAgentProtocolError, match="uncertainty marker"):
        normalize_constraint_response(request.request_id, request.raw_text, response)


def test_live_constraint_agent_requires_tracing() -> None:
    request = first_seed_request()
    with pytest.raises(ConstraintAgentConfigurationError, match="LANGSMITH_TRACING"):
        run_live_constraint_agent(request, make_settings(tracing=False))


def test_live_constraint_agent_uses_langsmith_context_and_flushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = first_seed_request()
    response = make_response(
        ConstraintProposalItem(
            kind="must_visit",
            value="故宫",
            strength="hard",
            priority=5,
            evidence="必须去故宫",
            evidence_mode="explicit",
        ),
        model="deepseek-v4-pro",
    )
    client = FakeLangSmithClient()
    observed_context: dict[str, object] = {}

    def fake_context(**kwargs: object) -> object:
        observed_context.update(kwargs)
        return nullcontext()

    monkeypatch.setattr(
        agent_module,
        "DeepSeekConstraintProposalModel",
        lambda _: FixedProposalModel(response),
    )
    monkeypatch.setattr(agent_module, "build_langsmith_client", lambda *_: client)
    monkeypatch.setattr(agent_module, "tracing_context", fake_context)

    result = run_live_constraint_agent(request, make_settings())

    assert result.model == "deepseek-v4-pro"
    assert observed_context["enabled"] is True
    assert observed_context["project_name"] == "eztrip-dev"
    assert client.flush_timeouts == [15.0]


def test_deepseek_constraint_model_requires_api_key() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key=None,
        langsmith_api_key=SecretStr("langsmith-test-secret-value"),
    )
    with pytest.raises(ConstraintAgentConfigurationError, match="DEEPSEEK_API_KEY"):
        DeepSeekConstraintProposalModel(settings)
