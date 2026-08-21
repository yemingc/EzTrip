import hashlib
from collections.abc import Mapping
from time import perf_counter
from typing import NotRequired, Protocol, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context
from langsmith.wrappers import wrap_openai
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import ValidationError

from app.agents.contracts import (
    ConstraintAgentResult,
    ConstraintDecision,
    ConstraintEvidenceMode,
    ConstraintModelResponse,
    ConstraintProposalBatch,
    ModelTokenUsage,
)
from app.core.config import Settings
from app.domain.request import (
    Constraint,
    ConstraintKind,
    ConstraintSet,
    ConstraintSource,
    TripRequest,
)
from app.observability.probe import build_langsmith_client, require_secret
from app.observability.redaction import TraceRedactor

CONSTRAINT_AGENT_NAME = "eztrip-constraint-agent-v1"
CONSTRAINT_AGENT_PROMPT_VERSION = "constraint-extraction-v1"
CONSTRAINT_PROPOSAL_TOOL_NAME = "submit_constraint_proposals"
UNCERTAINTY_MARKERS = (
    "模型推测",
    "可能",
    "也许",
    "尚未确认",
    "还没有确认",
    "未确认",
    "还没决定",
    "没决定",
)

SYSTEM_PROMPT = """你是 EzTrip 的约束抽取节点, 只从用户原话中提取旅行偏好与约束。
不要提取城市、日期、人数或预算字段; 不要补充原文没有表达的景点或偏好。
kind 只能使用给定枚举。must_visit/avoid/安全或无障碍刚需可为 hard, 普通偏好为 soft。
walking_intensity 的 value 只使用 low、medium、high; 其他 value 使用简短中文原值。
“偏好/喜欢历史文化、城市风光、亲子活动”等明确主题要提取为 soft interest。
“希望少走路/轻步行”仍是 soft walking_intensity; 只有“不能多走/必须少走”等刚需才是 hard。
evidence 必须逐字复制原文中的连续片段。
用户直接表达当前要求时 evidence_mode=explicit; 可能、建议、未确认或尚未决定时为 inferred。
evidence_mode 只控制确认状态, 不降低原约束强度。
“可能想去某地点”的 must_visit 提议仍为 hard + inferred。
“没有指定必去景点”不是 must_visit。没有可提取约束时返回空 items。
必须调用 submit_constraint_proposals, 不要输出正文。"""


class ConstraintAgentConfigurationError(RuntimeError):
    """Raised when a live constraint Agent dependency is not configured."""


class ConstraintAgentProtocolError(RuntimeError):
    """Raised when model output violates the Agent boundary."""


class ConstraintProposalModel(Protocol):
    def propose(self, raw_text: str) -> ConstraintModelResponse: ...


class ConstraintAgentState(TypedDict):
    request_id: str
    raw_text: str
    model_response: NotRequired[ConstraintModelResponse]
    result: NotRequired[ConstraintAgentResult]


CONSTRAINT_PROPOSAL_TOOL_SCHEMA = cast(
    ChatCompletionToolParam,
    {
        "type": "function",
        "function": {
            "name": CONSTRAINT_PROPOSAL_TOOL_NAME,
            "description": "提交从用户原话抽取的旅行约束提议。",
            "parameters": ConstraintProposalBatch.model_json_schema(mode="validation"),
        },
    },
)


class DeepSeekConstraintProposalModel:
    """DeepSeek tool-calling adapter; it cannot set source, confirmation, or IDs."""

    def __init__(self, settings: Settings) -> None:
        try:
            api_key = require_secret(settings.deepseek_api_key, "DEEPSEEK_API_KEY")
        except RuntimeError as error:
            raise ConstraintAgentConfigurationError(str(error)) from error
        client = OpenAI(
            api_key=api_key,
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._client = wrap_openai(client, chat_name="DeepSeekConstraintProposal")
        self._model = settings.deepseek_model

    def propose(self, raw_text: str) -> ConstraintModelResponse:
        messages = cast(
            list[ChatCompletionMessageParam],
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": raw_text},
            ],
        )
        started = perf_counter()
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=[CONSTRAINT_PROPOSAL_TOOL_SCHEMA],
            tool_choice={
                "type": "function",
                "function": {"name": CONSTRAINT_PROPOSAL_TOOL_NAME},
            },
            temperature=0,
            max_tokens=900,
            extra_body={"thinking": {"type": "disabled"}},
        )
        latency_ms = round((perf_counter() - started) * 1000)
        tool_calls = response.choices[0].message.tool_calls
        if tool_calls is None or len(tool_calls) != 1:
            raise ConstraintAgentProtocolError(
                "DeepSeek must return exactly one constraint proposal tool call"
            )
        tool_call = tool_calls[0]
        if tool_call.type != "function" or tool_call.function.name != CONSTRAINT_PROPOSAL_TOOL_NAME:
            raise ConstraintAgentProtocolError("DeepSeek returned an unexpected tool call")
        try:
            proposal = ConstraintProposalBatch.model_validate_json(tool_call.function.arguments)
        except ValidationError as error:
            raise ConstraintAgentProtocolError(
                "DeepSeek returned invalid constraint proposal arguments"
            ) from error

        usage = None
        if response.usage is not None:
            usage = ModelTokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )
        return ConstraintModelResponse(
            proposal=proposal,
            model=self._model,
            latency_ms=latency_ms,
            usage=usage,
        )


def canonicalize_constraint_value(kind: ConstraintKind, value: str) -> str:
    normalized = " ".join(value.split())
    if kind == ConstraintKind.WALKING_INTENSITY:
        aliases = {
            "low": "low",
            "轻步行": "low",
            "少走路": "low",
            "减少步行": "low",
            "medium": "medium",
            "适中": "medium",
            "high": "high",
            "大量步行": "high",
        }
        canonical = aliases.get(normalized.casefold())
        if canonical is None:
            raise ConstraintAgentProtocolError(
                "walking_intensity must normalize to low, medium, or high"
            )
        return canonical
    return normalized


def _constraint_id(
    kind: ConstraintKind,
    value: str,
    evidence_mode: ConstraintEvidenceMode,
) -> str:
    material = f"{kind.value}|{value.casefold()}|{evidence_mode.value}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"agent-{kind.value.replace('_', '-')}-{digest}"


def _raw_text_sha256(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def normalize_constraint_response(
    request_id: str,
    raw_text: str,
    response: ConstraintModelResponse,
) -> ConstraintAgentResult:
    decisions: list[ConstraintDecision] = []
    semantic_keys: set[tuple[ConstraintKind, str]] = set()
    for proposal in response.proposal.items:
        if proposal.evidence not in raw_text:
            raise ConstraintAgentProtocolError("constraint evidence is not an exact raw-text span")
        if proposal.evidence_mode == ConstraintEvidenceMode.EXPLICIT and any(
            marker in raw_text for marker in UNCERTAINTY_MARKERS
        ):
            raise ConstraintAgentProtocolError(
                "explicit proposal conflicts with an uncertainty marker in the raw text"
            )
        canonical_value = canonicalize_constraint_value(proposal.kind, proposal.value)
        semantic_key = (proposal.kind, canonical_value.casefold())
        if semantic_key in semantic_keys:
            raise ConstraintAgentProtocolError("duplicate semantic constraint proposal")
        semantic_keys.add(semantic_key)

        explicit = proposal.evidence_mode == ConstraintEvidenceMode.EXPLICIT
        try:
            constraint = Constraint(
                constraint_id=_constraint_id(
                    proposal.kind,
                    canonical_value,
                    proposal.evidence_mode,
                ),
                kind=proposal.kind,
                value=canonical_value,
                strength=proposal.strength,
                priority=proposal.priority,
                source=(
                    ConstraintSource.USER_EXPLICIT if explicit else ConstraintSource.AGENT_INFERRED
                ),
                confirmed=explicit,
            )
        except ValidationError as error:
            raise ConstraintAgentProtocolError("normalized constraint is invalid") from error
        decisions.append(
            ConstraintDecision(
                constraint=constraint,
                evidence=proposal.evidence,
                evidence_mode=proposal.evidence_mode,
            )
        )

    try:
        constraints = ConstraintSet(items=tuple(item.constraint for item in decisions))
    except ValidationError as error:
        raise ConstraintAgentProtocolError("normalized constraint set is invalid") from error
    return ConstraintAgentResult(
        request_id=request_id,
        raw_text_sha256=_raw_text_sha256(raw_text),
        model=response.model,
        latency_ms=response.latency_ms,
        usage=response.usage,
        decisions=tuple(decisions),
        constraints=constraints,
        hitl_constraint_ids=tuple(
            item.constraint.constraint_id
            for item in decisions
            if item.evidence_mode == ConstraintEvidenceMode.INFERRED
        ),
    )


def build_constraint_agent_graph(
    model: ConstraintProposalModel,
) -> CompiledStateGraph[
    ConstraintAgentState,
    None,
    ConstraintAgentState,
    ConstraintAgentState,
]:
    def propose_constraints(state: ConstraintAgentState) -> Mapping[str, ConstraintModelResponse]:
        return {"model_response": model.propose(state["raw_text"])}

    def validate_constraints(state: ConstraintAgentState) -> Mapping[str, ConstraintAgentResult]:
        response = state.get("model_response")
        if response is None:
            raise ConstraintAgentProtocolError("normalizer received no model response")
        return {
            "result": normalize_constraint_response(
                state["request_id"],
                state["raw_text"],
                response,
            )
        }

    workflow = StateGraph(ConstraintAgentState)
    workflow.add_node("propose_constraints", propose_constraints)
    workflow.add_node("validate_constraints", validate_constraints)
    workflow.add_edge(START, "propose_constraints")
    workflow.add_edge("propose_constraints", "validate_constraints")
    workflow.add_edge("validate_constraints", END)
    return workflow.compile(name=CONSTRAINT_AGENT_NAME)


def build_constraint_run_config(request_id: str, *, model: str) -> RunnableConfig:
    return {
        "run_name": CONSTRAINT_AGENT_NAME,
        "tags": ["ez-101", "constraint-agent", "schema-constrained"],
        "metadata": {
            "agent_version": "constraint-agent-v1",
            "prompt_version": CONSTRAINT_AGENT_PROMPT_VERSION,
            "request_id": request_id,
            "model": model,
            "raw_user_text_in_metadata": False,
        },
    }


def run_constraint_agent(
    request: TripRequest,
    model: ConstraintProposalModel,
) -> ConstraintAgentResult:
    graph = build_constraint_agent_graph(model)
    final_state = cast(
        ConstraintAgentState,
        graph.invoke(
            {"request_id": request.request_id, "raw_text": request.raw_text},
            config=build_constraint_run_config(request.request_id, model="injected-model"),
        ),
    )
    result = final_state.get("result")
    if result is None:
        raise ConstraintAgentProtocolError("constraint Agent completed without a result")
    return result


def replace_trip_request_constraints(
    request: TripRequest,
    result: ConstraintAgentResult,
) -> TripRequest:
    if result.request_id != request.request_id:
        raise ConstraintAgentProtocolError("Agent result request_id does not match TripRequest")
    if result.raw_text_sha256 != _raw_text_sha256(request.raw_text):
        raise ConstraintAgentProtocolError("Agent result raw-text hash does not match TripRequest")
    payload = request.model_dump(mode="json")
    payload["constraints"] = result.constraints.model_dump(mode="json")
    return TripRequest.model_validate(payload)


def run_live_constraint_agent(
    request: TripRequest,
    settings: Settings,
) -> ConstraintAgentResult:
    if not settings.langsmith_tracing:
        raise ConstraintAgentConfigurationError(
            "LANGSMITH_TRACING must be true for the live Constraint Agent"
        )
    redactor = TraceRedactor.from_settings(settings)
    try:
        langsmith_client = build_langsmith_client(settings, redactor)
    except RuntimeError as error:
        raise ConstraintAgentConfigurationError(str(error)) from error
    model = DeepSeekConstraintProposalModel(settings)
    graph = build_constraint_agent_graph(model)
    try:
        with tracing_context(
            enabled=True,
            client=langsmith_client,
            project_name=settings.langsmith_project,
        ):
            final_state = cast(
                ConstraintAgentState,
                graph.invoke(
                    {"request_id": request.request_id, "raw_text": request.raw_text},
                    config=build_constraint_run_config(
                        request.request_id,
                        model=settings.deepseek_model,
                    ),
                ),
            )
    finally:
        langsmith_client.flush(timeout=15.0)
    result = final_state.get("result")
    if result is None:
        raise ConstraintAgentProtocolError("live Constraint Agent completed without a result")
    return result
