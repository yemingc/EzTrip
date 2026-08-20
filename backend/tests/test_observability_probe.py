from typing import cast

import pytest

from app.observability.probe import (
    PROBE_CITY,
    PROBE_DATE,
    PROBE_REQUEST,
    ProbeModel,
    ProbeProtocolError,
    ProbeState,
    ProbeToolError,
    ToolRequest,
    WeatherObservation,
    build_probe_graph,
)


class FakeProbeModel:
    def __init__(self, tool_request: ToolRequest | None = None) -> None:
        self.calls: list[str] = []
        self.tool_request: ToolRequest = tool_request or {
            "id": "call_fixture_1",
            "name": "lookup_weather_fixture",
            "arguments": {"city": PROBE_CITY, "date": PROBE_DATE.isoformat()},
        }

    def choose_tool(self, request: str) -> ToolRequest:
        self.calls.append(f"choose:{request}")
        return self.tool_request

    def finalize(
        self,
        request: str,
        tool_request: ToolRequest,
        tool_result: WeatherObservation,
    ) -> str:
        self.calls.append(f"finalize:{tool_request['name']}:{tool_result['source']}")
        return "第二天有显著降雨风险, 建议调整户外活动。"


def invoke_probe(model: FakeProbeModel, *, force_tool_error: bool = False) -> ProbeState:
    graph = build_probe_graph(cast(ProbeModel, model))
    return graph.invoke({"request": PROBE_REQUEST, "force_tool_error": force_tool_error})


def test_probe_runs_three_named_nodes_with_a_fixture_tool() -> None:
    model = FakeProbeModel()
    graph = build_probe_graph(cast(ProbeModel, model))

    node_names = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    result = graph.invoke({"request": PROBE_REQUEST, "force_tool_error": False})

    assert node_names == {
        "request_weather_tool",
        "execute_weather_fixture",
        "finalize_probe",
    }
    assert result["tool_result"] == {
        "city": PROBE_CITY,
        "date": PROBE_DATE.isoformat(),
        "condition": "rain",
        "precipitation_probability": 0.9,
        "source": "probe_weather_fixture",
        "retrieved_at": "2026-10-01T08:00:00+08:00",
    }
    assert result["answer"].startswith("第二天有显著降雨风险")
    assert model.calls == [
        f"choose:{PROBE_REQUEST}",
        "finalize:lookup_weather_fixture:probe_weather_fixture",
    ]


def test_probe_records_a_controlled_tool_failure_without_finalizing() -> None:
    model = FakeProbeModel()

    with pytest.raises(ProbeToolError, match="Controlled fixture failure"):
        invoke_probe(model, force_tool_error=True)

    assert model.calls == [f"choose:{PROBE_REQUEST}"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"city": "上海市", "date": PROBE_DATE.isoformat()},
        {"city": PROBE_CITY, "date": "2026-10-04"},
    ],
)
def test_probe_rejects_model_changes_to_fixed_tool_arguments(
    arguments: dict[str, str],
) -> None:
    model = FakeProbeModel(
        {
            "id": "call_fixture_1",
            "name": "lookup_weather_fixture",
            "arguments": arguments,
        }
    )

    with pytest.raises(ProbeProtocolError, match="changed the fixed probe request"):
        invoke_probe(model)

    assert model.calls == [f"choose:{PROBE_REQUEST}"]
