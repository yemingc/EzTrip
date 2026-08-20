import json
from collections.abc import Mapping
from datetime import date
from typing import NotRequired, Protocol, TypedDict, cast
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langsmith import Client, traceable, tracing_context
from langsmith.wrappers import wrap_openai
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.core.config import Settings
from app.observability.redaction import TraceRedactor

PROBE_NAME = "eztrip-observability-probe-v1"
PROBE_VERSION = "1"
WEATHER_TOOL_NAME = "lookup_weather_fixture"
PROBE_CITY = "北京市"
PROBE_DATE = date(2026, 10, 3)
PROBE_REQUEST = f"请调用天气工具检查{PROBE_CITY} {PROBE_DATE.isoformat()} 的户外活动风险。"


class ProbeConfigurationError(RuntimeError):
    """Raised when a live probe dependency is not configured."""


class ProbeProtocolError(RuntimeError):
    """Raised when the model violates the probe tool contract."""


class ProbeToolError(RuntimeError):
    """Raised by the controlled failure path of the fixture tool."""


class ToolRequest(TypedDict):
    id: str
    name: str
    arguments: dict[str, str]


class WeatherObservation(TypedDict):
    city: str
    date: str
    condition: str
    precipitation_probability: float
    source: str
    retrieved_at: str


class ProbeState(TypedDict):
    request: str
    force_tool_error: bool
    tool_request: NotRequired[ToolRequest]
    tool_result: NotRequired[WeatherObservation]
    answer: NotRequired[str]


class ProbeRunResult(TypedDict):
    answer: str
    model: str
    project: str
    trace_id: str


class WeatherToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str = Field(min_length=1, max_length=50)
    date: date


class ProbeModel(Protocol):
    def choose_tool(self, request: str) -> ToolRequest: ...

    def finalize(
        self,
        request: str,
        tool_request: ToolRequest,
        tool_result: WeatherObservation,
    ) -> str: ...


WEATHER_TOOL_SCHEMA: ChatCompletionToolParam = {
    "type": "function",
    "function": {
        "name": WEATHER_TOOL_NAME,
        "description": "读取版本化天气 fixture, 仅用于 EzTrip Gate 0 可观测性探针。",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "中国城市名称"},
                "date": {"type": "string", "description": "ISO 8601 日期, 格式 YYYY-MM-DD"},
            },
            "required": ["city", "date"],
            "additionalProperties": False,
        },
    },
}


class DeepSeekProbeModel:
    """Minimal DeepSeek adapter used only by the observability probe."""

    def __init__(self, settings: Settings) -> None:
        api_key = require_secret(settings.deepseek_api_key, "DEEPSEEK_API_KEY")
        client = OpenAI(
            api_key=api_key,
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._client = wrap_openai(client, chat_name="DeepSeekChatCompletion")
        self._model = settings.deepseek_model

    def choose_tool(self, request: str) -> ToolRequest:
        messages = cast(
            list[ChatCompletionMessageParam],
            [
                {
                    "role": "system",
                    "content": (
                        "你是 EzTrip 可观测性探针。必须调用指定天气工具, 不得根据模型记忆回答天气。"
                    ),
                },
                {"role": "user", "content": request},
            ],
        )
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=[WEATHER_TOOL_SCHEMA],
            tool_choice={"type": "function", "function": {"name": WEATHER_TOOL_NAME}},
            temperature=0,
            max_tokens=160,
            extra_body={"thinking": {"type": "disabled"}},
        )
        tool_calls = response.choices[0].message.tool_calls
        if tool_calls is None or len(tool_calls) != 1:
            raise ProbeProtocolError("DeepSeek must return exactly one weather tool call")
        tool_call = tool_calls[0]
        if tool_call.type != "function":
            raise ProbeProtocolError("DeepSeek returned a non-function tool call")
        if tool_call.function.name != WEATHER_TOOL_NAME:
            raise ProbeProtocolError("DeepSeek selected an unexpected tool")
        try:
            arguments = WeatherToolArguments.model_validate_json(tool_call.function.arguments)
        except ValidationError as error:
            raise ProbeProtocolError("DeepSeek returned invalid weather tool arguments") from error
        return {
            "id": tool_call.id,
            "name": tool_call.function.name,
            "arguments": {
                "city": arguments.city,
                "date": arguments.date.isoformat(),
            },
        }

    def finalize(
        self,
        request: str,
        tool_request: ToolRequest,
        tool_result: WeatherObservation,
    ) -> str:
        messages = cast(
            list[ChatCompletionMessageParam],
            [
                {
                    "role": "system",
                    "content": "只根据工具结果, 用一句中文说明户外活动天气风险。",
                },
                {"role": "user", "content": request},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_request["id"],
                            "type": "function",
                            "function": {
                                "name": tool_request["name"],
                                "arguments": json.dumps(
                                    tool_request["arguments"],
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": tool_request["id"],
                    "content": json.dumps(tool_result, ensure_ascii=False, sort_keys=True),
                },
            ],
        )
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=0,
            max_tokens=160,
            extra_body={"thinking": {"type": "disabled"}},
        )
        content = response.choices[0].message.content
        if content is None or not content.strip():
            raise ProbeProtocolError("DeepSeek returned an empty probe summary")
        return content.strip()


def require_secret(secret: SecretStr | None, variable_name: str) -> str:
    if secret is None:
        raise ProbeConfigurationError(f"{variable_name} is required for the live probe")
    value = secret.get_secret_value().strip()
    if not value:
        raise ProbeConfigurationError(f"{variable_name} is required for the live probe")
    return value


@traceable(run_type="tool", name="probe_weather_fixture")
def lookup_weather_fixture(
    city: str,
    requested_date: str,
    *,
    force_error: bool = False,
) -> WeatherObservation:
    if force_error:
        raise ProbeToolError("Controlled fixture failure for LangSmith error tracing")
    return {
        "city": city,
        "date": requested_date,
        "condition": "rain",
        "precipitation_probability": 0.9,
        "source": "probe_weather_fixture",
        "retrieved_at": "2026-10-01T08:00:00+08:00",
    }


def build_probe_graph(
    model: ProbeModel,
) -> CompiledStateGraph[ProbeState, None, ProbeState, ProbeState]:
    def request_weather_tool(state: ProbeState) -> Mapping[str, ToolRequest]:
        return {"tool_request": model.choose_tool(state["request"])}

    def execute_weather_fixture(state: ProbeState) -> Mapping[str, WeatherObservation]:
        request = state.get("tool_request")
        if request is None:
            raise ProbeProtocolError("Weather tool node received no tool request")
        arguments = request["arguments"]
        if arguments["city"] != PROBE_CITY or arguments["date"] != PROBE_DATE.isoformat():
            raise ProbeProtocolError("Weather tool arguments changed the fixed probe request")
        observation = lookup_weather_fixture(
            arguments["city"],
            arguments["date"],
            force_error=state["force_tool_error"],
        )
        return {"tool_result": observation}

    def finalize_probe(state: ProbeState) -> Mapping[str, str]:
        request = state.get("tool_request")
        result = state.get("tool_result")
        if request is None or result is None:
            raise ProbeProtocolError("Finalizer received incomplete probe state")
        return {"answer": model.finalize(state["request"], request, result)}

    workflow = StateGraph(ProbeState)
    workflow.add_node("request_weather_tool", request_weather_tool)
    workflow.add_node("execute_weather_fixture", execute_weather_fixture)
    workflow.add_node("finalize_probe", finalize_probe)
    workflow.add_edge(START, "request_weather_tool")
    workflow.add_edge("request_weather_tool", "execute_weather_fixture")
    workflow.add_edge("execute_weather_fixture", "finalize_probe")
    workflow.add_edge("finalize_probe", END)
    return workflow.compile(name=PROBE_NAME)


def build_langsmith_client(settings: Settings, redactor: TraceRedactor) -> Client:
    api_key = require_secret(settings.langsmith_api_key, "LANGSMITH_API_KEY")
    workspace_id = settings.langsmith_workspace_id
    return Client(
        api_key=api_key,
        api_url=settings.langsmith_endpoint.rstrip("/"),
        workspace_id=workspace_id.strip() if workspace_id and workspace_id.strip() else None,
        anonymizer=redactor.anonymize_run,
    )


def run_live_probe(
    settings: Settings,
    *,
    force_tool_error: bool = False,
    run_id: UUID | None = None,
) -> ProbeRunResult:
    if not settings.langsmith_tracing:
        raise ProbeConfigurationError("LANGSMITH_TRACING must be true for the live probe")

    redactor = TraceRedactor.from_settings(settings)
    langsmith_client = build_langsmith_client(settings, redactor)
    model = DeepSeekProbeModel(settings)
    graph = build_probe_graph(model)
    trace_id = run_id or uuid4()
    safe_request = redactor.redact_text(PROBE_REQUEST)
    metadata = {
        "probe_version": PROBE_VERSION,
        "data_mode": "fixture",
        "llm_provider": settings.llm_provider,
        "model": settings.deepseek_model,
        "contains_live_travel_data": False,
    }

    try:
        with tracing_context(
            enabled=True,
            client=langsmith_client,
            project_name=settings.langsmith_project,
        ):
            final_state = graph.invoke(
                {"request": safe_request, "force_tool_error": force_tool_error},
                config={
                    "run_id": trace_id,
                    "run_name": PROBE_NAME,
                    "tags": ["gate-0", "observability-probe", "fixture"],
                    "metadata": metadata,
                },
            )
    finally:
        langsmith_client.flush(timeout=15.0)

    answer = final_state.get("answer")
    if answer is None:
        raise ProbeProtocolError("Probe graph completed without an answer")
    return {
        "answer": answer,
        "model": settings.deepseek_model,
        "project": settings.langsmith_project,
        "trace_id": str(trace_id),
    }
