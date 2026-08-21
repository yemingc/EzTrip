import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
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
    ModelTokenUsage,
    PlannerModelResponse,
    PlannerPlacementDecision,
    PlannerProposalBatch,
    SinglePlannerAgentResult,
)
from app.core.config import Settings
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerContext
from app.domain.planning import ActivityKind, DayPlan, ItineraryItem
from app.observability.probe import build_langsmith_client, require_secret
from app.observability.redaction import TraceRedactor

SINGLE_PLANNER_NAME = "eztrip-single-planner-v1"
SINGLE_PLANNER_PROMPT_VERSION = "candidate-placement-v1"
PLANNER_PROPOSAL_TOOL_NAME = "submit_candidate_placements"
DEFAULT_ACTIVITY_DURATION_MINUTES = 120
CHINA_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")

SYSTEM_PROMPT = """你是 EzTrip 的单 Planner 基线节点, 只安排输入中明确给出的候选景点。
必须把每个候选恰好安排一次, 不得新增、改名或遗漏 candidate_id。
只决定 day_number、start_time 和简短 reason; 日期、标题、来源、结束时间与稳定 ID 由代码生成。
day_number 必须来自给定日期, start_time 只能是 08:00 到 21:30 的整点或半点。
不要声称知道实时营业时间、路线、票价、酒店、天气或预算可行性。
如果约束不足以判断最佳日期, 选择合理的日期并在 reason 中说明这是待后续路线/天气校验的草案。
必须调用 submit_candidate_placements, 不要输出正文。"""


class SinglePlannerConfigurationError(RuntimeError):
    """Raised when a live single-Planner dependency is not configured."""


class SinglePlannerProtocolError(RuntimeError):
    """Raised when a Planner proposal violates deterministic grounding rules."""


class PlannerProposalModel(Protocol):
    def propose(
        self,
        context: PlannerContext,
        candidates: tuple[CandidatePOI, ...],
    ) -> PlannerModelResponse: ...


class SinglePlannerState(TypedDict):
    context: PlannerContext
    candidates: tuple[CandidatePOI, ...]
    model_response: NotRequired[PlannerModelResponse]
    result: NotRequired[SinglePlannerAgentResult]


PLANNER_PROPOSAL_TOOL_SCHEMA = cast(
    ChatCompletionToolParam,
    {
        "type": "function",
        "function": {
            "name": PLANNER_PROPOSAL_TOOL_NAME,
            "description": "提交对已给定候选景点的逐日放置提案。",
            "parameters": PlannerProposalBatch.model_json_schema(mode="validation"),
        },
    },
)


def _planner_input_payload(
    context: PlannerContext,
    candidates: tuple[CandidatePOI, ...],
) -> str:
    constraints = (
        *context.confirmed_hard_constraints,
        *context.confirmed_soft_constraints,
    )
    payload = {
        "destination": context.destination.normalized_name,
        "days": [
            {"day_number": item.day_number, "date": item.date.isoformat()} for item in context.days
        ],
        "party": {
            "adults": context.party.adults,
            "children": context.party.children,
            "seniors": context.party.seniors,
        },
        "travel_styles": list(context.travel_styles),
        "confirmed_constraints": [
            {
                "kind": item.kind.value,
                "value": item.value,
                "strength": item.strength.value,
                "priority": item.priority,
                "applies_to_dates": [value.isoformat() for value in item.applies_to_dates],
            }
            for item in constraints
        ],
        "candidates": [
            {
                "candidate_id": item.candidate_id,
                "name": item.name,
                "district": item.district,
                "environment": item.environment.value,
                "suggested_duration_minutes": item.suggested_duration_minutes,
                "tags": list(item.tags),
            }
            for item in candidates
        ],
        "known_boundaries": {
            "route_data_available": False,
            "weather_data_available": False,
            "opening_hours_verified": False,
            "budget_validated": False,
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class DeepSeekPlannerProposalModel:
    """DeepSeek adapter whose schema cannot set candidate facts or source metadata."""

    def __init__(self, settings: Settings) -> None:
        try:
            api_key = require_secret(settings.deepseek_api_key, "DEEPSEEK_API_KEY")
        except RuntimeError as error:
            raise SinglePlannerConfigurationError(str(error)) from error
        client = OpenAI(
            api_key=api_key,
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._client = wrap_openai(client, chat_name="DeepSeekSinglePlannerProposal")
        self._model = settings.deepseek_model

    def propose(
        self,
        context: PlannerContext,
        candidates: tuple[CandidatePOI, ...],
    ) -> PlannerModelResponse:
        messages = cast(
            list[ChatCompletionMessageParam],
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _planner_input_payload(context, candidates)},
            ],
        )
        started = perf_counter()
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=[PLANNER_PROPOSAL_TOOL_SCHEMA],
            tool_choice={
                "type": "function",
                "function": {"name": PLANNER_PROPOSAL_TOOL_NAME},
            },
            temperature=0,
            max_tokens=900,
            extra_body={"thinking": {"type": "disabled"}},
        )
        latency_ms = round((perf_counter() - started) * 1000)
        tool_calls = response.choices[0].message.tool_calls
        if tool_calls is None or len(tool_calls) != 1:
            raise SinglePlannerProtocolError(
                "DeepSeek must return exactly one Planner proposal tool call"
            )
        tool_call = tool_calls[0]
        if tool_call.type != "function" or tool_call.function.name != PLANNER_PROPOSAL_TOOL_NAME:
            raise SinglePlannerProtocolError("DeepSeek returned an unexpected Planner tool call")
        try:
            proposal = PlannerProposalBatch.model_validate_json(tool_call.function.arguments)
        except ValidationError as error:
            raise SinglePlannerProtocolError(
                "DeepSeek returned invalid Planner proposal arguments"
            ) from error

        usage = None
        if response.usage is not None:
            usage = ModelTokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )
        return PlannerModelResponse(
            proposal=proposal,
            model=self._model,
            latency_ms=latency_ms,
            usage=usage,
        )


def candidate_set_sha256(candidates: tuple[CandidatePOI, ...]) -> str:
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in candidates],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _item_id(context: PlannerContext, candidate_id: str, starts_at: datetime) -> str:
    material = f"{context.context_id}|{candidate_id}|{starts_at.isoformat()}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"planner-item-{digest}"


def normalize_planner_response(
    context: PlannerContext,
    candidates: tuple[CandidatePOI, ...],
    response: PlannerModelResponse,
) -> SinglePlannerAgentResult:
    if not candidates:
        raise SinglePlannerProtocolError("single Planner requires at least one provider candidate")
    candidates_by_id = {item.candidate_id: item for item in candidates}
    if len(candidates_by_id) != len(candidates):
        raise SinglePlannerProtocolError("single Planner received duplicate candidate ids")
    proposal_ids = [item.candidate_id for item in response.proposal.items]
    if len(proposal_ids) != len(set(proposal_ids)):
        raise SinglePlannerProtocolError("Planner proposal repeats a candidate id")
    if set(proposal_ids) != set(candidates_by_id):
        raise SinglePlannerProtocolError(
            "Planner proposal must place every provided candidate exactly once"
        )

    decisions_by_date: defaultdict[date, list[PlannerPlacementDecision]] = defaultdict(list)
    valid_days = {item.day_number: item.date for item in context.days}
    for proposal in response.proposal.items:
        day = valid_days.get(proposal.day_number)
        if day is None:
            raise SinglePlannerProtocolError("Planner proposal references a day outside the trip")
        candidate = candidates_by_id[proposal.candidate_id]
        if candidate.city != context.destination.normalized_name:
            raise SinglePlannerProtocolError("Planner candidate city does not match the context")
        hour, minute = (int(value) for value in proposal.start_time.split(":"))
        starts_at = datetime.combine(day, time(hour, minute), tzinfo=CHINA_TIMEZONE)
        duration = candidate.suggested_duration_minutes or DEFAULT_ACTIVITY_DURATION_MINUTES
        ends_at = starts_at + timedelta(minutes=duration)
        if ends_at.date() != day:
            raise SinglePlannerProtocolError("Planner activity cannot cross the day boundary")
        item = ItineraryItem(
            item_id=_item_id(context, candidate.candidate_id, starts_at),
            kind=ActivityKind.ATTRACTION,
            title=candidate.name,
            start_at=starts_at,
            end_at=ends_at,
            candidate_id=candidate.candidate_id,
            source=candidate.source,
            notes=(
                "单 Planner V1 仅生成候选放置草案, 模型排程理由保留在 decision 中供审计。",
                "活动时长为候选建议值或 V1 固定 120 分钟草案, 尚未校验实时营业时间。",
            ),
        )
        decisions_by_date[day].append(PlannerPlacementDecision(proposal=proposal, item=item))

    ordered_decisions: list[PlannerPlacementDecision] = []
    day_plans: list[DayPlan] = []
    for day in sorted(decisions_by_date):
        decisions = sorted(decisions_by_date[day], key=lambda item: item.item.start_at)
        try:
            day_plan = DayPlan(date=day, items=tuple(item.item for item in decisions))
        except ValidationError as error:
            raise SinglePlannerProtocolError(
                "Planner proposal creates an invalid timeline"
            ) from error
        ordered_decisions.extend(decisions)
        day_plans.append(day_plan)

    return SinglePlannerAgentResult(
        request_id=context.request_id,
        context_id=context.context_id,
        input_candidates_sha256=candidate_set_sha256(candidates),
        model=response.model,
        latency_ms=response.latency_ms,
        usage=response.usage,
        decisions=tuple(ordered_decisions),
        day_plans=tuple(day_plans),
    )


def build_single_planner_graph(
    model: PlannerProposalModel,
) -> CompiledStateGraph[
    SinglePlannerState,
    None,
    SinglePlannerState,
    SinglePlannerState,
]:
    def propose_schedule(state: SinglePlannerState) -> Mapping[str, PlannerModelResponse]:
        return {"model_response": model.propose(state["context"], state["candidates"])}

    def validate_schedule(state: SinglePlannerState) -> Mapping[str, SinglePlannerAgentResult]:
        response = state.get("model_response")
        if response is None:
            raise SinglePlannerProtocolError("Planner normalizer received no model response")
        return {
            "result": normalize_planner_response(
                state["context"],
                state["candidates"],
                response,
            )
        }

    workflow = StateGraph(SinglePlannerState)
    workflow.add_node("propose_schedule", propose_schedule)
    workflow.add_node("validate_schedule", validate_schedule)
    workflow.add_edge(START, "propose_schedule")
    workflow.add_edge("propose_schedule", "validate_schedule")
    workflow.add_edge("validate_schedule", END)
    return workflow.compile(checkpointer=False, name=SINGLE_PLANNER_NAME)


def build_single_planner_run_config(context: PlannerContext, *, model: str) -> RunnableConfig:
    return {
        "run_name": SINGLE_PLANNER_NAME,
        "tags": ["ez-102", "single-planner", "schema-constrained"],
        "metadata": {
            "agent_version": "single-planner-v1",
            "prompt_version": SINGLE_PLANNER_PROMPT_VERSION,
            "request_id": context.request_id,
            "context_id": context.context_id,
            "model": model,
            "raw_user_text_in_metadata": False,
        },
    }


def run_single_planner(
    context: PlannerContext,
    candidates: tuple[CandidatePOI, ...],
    model: PlannerProposalModel,
) -> SinglePlannerAgentResult:
    graph = build_single_planner_graph(model)
    final_state = cast(
        SinglePlannerState,
        graph.invoke(
            {"context": context, "candidates": candidates},
            config=build_single_planner_run_config(context, model="injected-model"),
        ),
    )
    result = final_state.get("result")
    if result is None:
        raise SinglePlannerProtocolError("single Planner completed without a result")
    return result


def run_live_single_planner(
    context: PlannerContext,
    candidates: tuple[CandidatePOI, ...],
    settings: Settings,
) -> SinglePlannerAgentResult:
    if not settings.langsmith_tracing:
        raise SinglePlannerConfigurationError(
            "LANGSMITH_TRACING must be true for the live single Planner"
        )
    redactor = TraceRedactor.from_settings(settings)
    try:
        langsmith_client = build_langsmith_client(settings, redactor)
    except RuntimeError as error:
        raise SinglePlannerConfigurationError(str(error)) from error
    model = DeepSeekPlannerProposalModel(settings)
    graph = build_single_planner_graph(model)
    try:
        with tracing_context(
            enabled=True,
            client=langsmith_client,
            project_name=settings.langsmith_project,
        ):
            final_state = cast(
                SinglePlannerState,
                graph.invoke(
                    {"context": context, "candidates": candidates},
                    config=build_single_planner_run_config(
                        context,
                        model=settings.deepseek_model,
                    ),
                ),
            )
    finally:
        langsmith_client.flush(timeout=15.0)
    result = final_state.get("result")
    if result is None:
        raise SinglePlannerProtocolError("live single Planner completed without a result")
    return result
