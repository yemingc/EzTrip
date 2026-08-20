from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.observability import probe as probe_module
from app.observability.probe import (
    PROBE_CITY,
    PROBE_DATE,
    PROBE_REQUEST,
    DeepSeekProbeModel,
    ProbeConfigurationError,
    ToolRequest,
    WeatherObservation,
    require_secret,
    run_live_probe,
)


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


class FakeProbeModel:
    def choose_tool(self, request: str) -> ToolRequest:
        assert request == PROBE_REQUEST
        return {
            "id": "call_fixture_1",
            "name": "lookup_weather_fixture",
            "arguments": {"city": PROBE_CITY, "date": PROBE_DATE.isoformat()},
        }

    def finalize(
        self,
        request: str,
        tool_request: ToolRequest,
        tool_result: WeatherObservation,
    ) -> str:
        assert request == PROBE_REQUEST
        assert tool_request["name"] == "lookup_weather_fixture"
        assert tool_result["source"] == "probe_weather_fixture"
        return "fixture summary"


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


def tool_call_response(*, arguments: str) -> object:
    tool_call = SimpleNamespace(
        id="call_fixture_1",
        type="function",
        function=SimpleNamespace(name="lookup_weather_fixture", arguments=arguments),
    )
    message = SimpleNamespace(tool_calls=[tool_call], content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def text_response(content: str | None) -> object:
    message = SimpleNamespace(tool_calls=None, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_deepseek_adapter_validates_tool_arguments_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeOpenAIClient(
        [
            tool_call_response(
                arguments=f'{{"city":"{PROBE_CITY}","date":"{PROBE_DATE.isoformat()}"}}'
            ),
            text_response("  第二天有雨。  "),
        ]
    )
    constructor_arguments: dict[str, object] = {}

    def fake_openai(**kwargs: object) -> FakeOpenAIClient:
        constructor_arguments.update(kwargs)
        return fake_client

    monkeypatch.setattr(probe_module, "OpenAI", fake_openai)
    monkeypatch.setattr(probe_module, "wrap_openai", lambda client, **_: client)

    model = DeepSeekProbeModel(make_settings())
    tool_request = model.choose_tool(PROBE_REQUEST)
    answer = model.finalize(
        PROBE_REQUEST,
        tool_request,
        {
            "city": PROBE_CITY,
            "date": PROBE_DATE.isoformat(),
            "condition": "rain",
            "precipitation_probability": 0.9,
            "source": "probe_weather_fixture",
            "retrieved_at": "2026-10-01T08:00:00+08:00",
        },
    )

    assert constructor_arguments["base_url"] == "https://api.deepseek.com"
    assert constructor_arguments["timeout"] == 30.0
    assert constructor_arguments["max_retries"] == 1
    assert tool_request["arguments"] == {
        "city": PROBE_CITY,
        "date": PROBE_DATE.isoformat(),
    }
    assert fake_client.completions.calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "lookup_weather_fixture"},
    }
    assert answer == "第二天有雨。"


def test_live_probe_uses_context_metadata_and_flushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeLangSmithClient()
    observed_context: dict[str, object] = {}

    def fake_context(**kwargs: object) -> object:
        observed_context.update(kwargs)
        return nullcontext()

    monkeypatch.setattr(probe_module, "DeepSeekProbeModel", lambda _: FakeProbeModel())
    monkeypatch.setattr(probe_module, "build_langsmith_client", lambda *_: client)
    monkeypatch.setattr(probe_module, "tracing_context", fake_context)
    trace_id = UUID("11111111-1111-4111-8111-111111111111")

    result = run_live_probe(make_settings(), run_id=trace_id)

    assert result == {
        "answer": "fixture summary",
        "model": "deepseek-v4-pro",
        "project": "eztrip-dev",
        "trace_id": str(trace_id),
    }
    assert observed_context["enabled"] is True
    assert observed_context["project_name"] == "eztrip-dev"
    assert client.flush_timeouts == [15.0]


def test_live_probe_rejects_disabled_tracing() -> None:
    with pytest.raises(ProbeConfigurationError, match="LANGSMITH_TRACING"):
        run_live_probe(make_settings(tracing=False))


@pytest.mark.parametrize("secret", [None, SecretStr("")])
def test_require_secret_rejects_missing_values(secret: SecretStr | None) -> None:
    with pytest.raises(ProbeConfigurationError, match="TEST_SECRET"):
        require_secret(secret, "TEST_SECRET")
