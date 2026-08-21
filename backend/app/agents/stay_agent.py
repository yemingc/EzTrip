import hashlib
import json
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
    ModelTokenUsage,
    StayAgentResult,
    StayCandidateObservation,
    StayCandidateSelectionProposal,
    StayEvidenceKind,
    StayQueryModelResponse,
    StayQueryProposalBatch,
    StayRecommendation,
    StaySearchQuery,
    StaySelectionModelResponse,
    StaySelectionProposalBatch,
)
from app.agents.hashing import stay_candidate_set_sha256
from app.core.config import Settings
from app.domain.candidates import CandidateStay
from app.domain.context import PlannerCapability, PlannerContext
from app.observability.probe import build_langsmith_client, require_secret
from app.observability.redaction import TraceRedactor
from app.providers.ports import StaySearchProvider, StaySearchRequest

STAY_AGENT_NAME = "eztrip-stay-agent-v1"
STAY_QUERY_PROMPT_VERSION = "stay-query-strategy-v1"
STAY_SELECTION_PROMPT_VERSION = "stay-candidate-selection-v1"
STAY_QUERY_TOOL_NAME = "submit_stay_queries"
STAY_SELECTION_TOOL_NAME = "submit_stay_selection"
MAX_CANDIDATES_PER_QUERY = 3

QUERY_SYSTEM_PROMPT = """你是 EzTrip Stay Agent 的住宿搜索策略节点。
只根据结构化 PlannerContext 设计 1 到 3 条高德住宿 POI 文本搜索策略。
target_area 表示希望覆盖的区域方向, keywords 是实际提交给 Provider 的住宿搜索词。
搜索要覆盖已确认的住宿约束、旅行风格和同行人特征; 不要自行添加用户没有表达的硬约束。
context_refs 只能复制输入 allowed_context_refs 中的完整字符串; 没有对应依据时留空。
只决定 target_area、keywords、reason 和 context_refs。
不得生成酒店事实、候选 ID、坐标、房价、实时库存、评分、设施、预订或退款信息。
不得分配住宿预算, 也不得将高德 POI 结果解释为可订房源。
必须调用 submit_stay_queries, 不要输出正文。"""

SELECTION_SYSTEM_PROMPT = """你是 EzTrip Stay Agent 的住宿候选筛选节点。
只能从输入 provider_candidates 中选择 1 到 6 个 candidate_id, 并给出从 1 开始连续的 rank。
这是住宿区域与偏好相关性筛选, 不是候选全量覆盖任务; 有合适候选时也可以只选 1 个。
不得为了凑数选择与 confirmed_constraints、travel_styles 或同行人特征没有直接关系的候选。
不得新增候选、改名、改区域或补充未提供的事实。每条推荐必须提供至少一个可核验 evidence:
query_match 只能引用候选的 query_ids; area_name、district、tag 只能逐字复制候选字段。
reason 只解释候选与已给偏好或区域策略的匹配。不得声称知道房价、实时库存、房型、评分、设施、
取消政策、预订能力、路线、天气或预算可行性。
必须调用 submit_stay_selection, 不要输出正文。"""


class StayAgentConfigurationError(RuntimeError):
    """Raised when a live Stay Agent dependency is not configured."""


class StayAgentProtocolError(RuntimeError):
    """Raised when model or provider output violates the Stay grounding boundary."""


class StayProposalModel(Protocol):
    def propose_queries(self, context: PlannerContext) -> StayQueryModelResponse: ...

    def select_candidates(
        self,
        context: PlannerContext,
        queries: tuple[StaySearchQuery, ...],
        observations: tuple[StayCandidateObservation, ...],
    ) -> StaySelectionModelResponse: ...


class StayAgentState(TypedDict):
    context: PlannerContext
    query_response: NotRequired[StayQueryModelResponse]
    queries: NotRequired[tuple[StaySearchQuery, ...]]
    observations: NotRequired[tuple[StayCandidateObservation, ...]]
    selection_response: NotRequired[StaySelectionModelResponse]
    result: NotRequired[StayAgentResult]


STAY_QUERY_TOOL_SCHEMA = cast(
    ChatCompletionToolParam,
    {
        "type": "function",
        "function": {
            "name": STAY_QUERY_TOOL_NAME,
            "description": "提交基于结构化旅行偏好的住宿 POI 搜索策略。",
            "parameters": StayQueryProposalBatch.model_json_schema(mode="validation"),
        },
    },
)

STAY_SELECTION_TOOL_SCHEMA = cast(
    ChatCompletionToolParam,
    {
        "type": "function",
        "function": {
            "name": STAY_SELECTION_TOOL_NAME,
            "description": "提交对住宿 Provider 候选的可追溯筛选与排序。",
            "parameters": StaySelectionProposalBatch.model_json_schema(mode="validation"),
        },
    },
)


def allowed_stay_context_refs(context: PlannerContext) -> tuple[str, ...]:
    constraint_refs = tuple(
        f"constraint:{item.constraint_id}"
        for item in (
            *context.confirmed_hard_constraints,
            *context.confirmed_soft_constraints,
        )
    )
    style_refs = tuple(f"travel_style:{item}" for item in context.travel_styles)
    party_refs: list[str] = []
    if context.party.children:
        party_refs.append("party:children")
    if context.party.seniors:
        party_refs.append("party:seniors")
    if context.party.rooms is not None:
        party_refs.append(f"party:rooms:{context.party.rooms}")
    party_refs.append(f"trip:lodging_nights:{context.lodging_nights}")
    return (*constraint_refs, *style_refs, *party_refs)


def _query_input_payload(context: PlannerContext) -> str:
    payload = {
        "destination": {
            "name": context.destination.normalized_name,
            "administrative_code": context.destination.administrative_code,
        },
        "trip": {
            "day_count": context.day_count,
            "lodging_nights": context.lodging_nights,
            "party": {
                "adults": context.party.adults,
                "children": context.party.children,
                "seniors": context.party.seniors,
                "rooms": context.party.rooms,
                "room_nights": context.party.room_nights,
            },
        },
        "travel_styles": list(context.travel_styles),
        "confirmed_constraints": [
            {
                "constraint_id": item.constraint_id,
                "kind": item.kind.value,
                "value": item.value,
                "strength": item.strength.value,
                "priority": item.priority,
            }
            for item in (
                *context.confirmed_hard_constraints,
                *context.confirmed_soft_constraints,
            )
        ],
        "allowed_context_refs": list(allowed_stay_context_refs(context)),
        "known_boundaries": {
            "poi_search_only": True,
            "lodging_budget_present": bool(
                context.budget is not None and context.budget.includes_lodging
            ),
            "price_lookup_in_scope": False,
            "availability_lookup_in_scope": False,
            "booking_in_scope": False,
            "route_and_weather_validation_in_scope": False,
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _selection_input_payload(
    context: PlannerContext,
    queries: tuple[StaySearchQuery, ...],
    observations: tuple[StayCandidateObservation, ...],
) -> str:
    payload = {
        "destination": context.destination.normalized_name,
        "party": {
            "adults": context.party.adults,
            "children": context.party.children,
            "seniors": context.party.seniors,
            "rooms": context.party.rooms,
            "lodging_nights": context.lodging_nights,
        },
        "travel_styles": list(context.travel_styles),
        "confirmed_constraints": [
            {
                "constraint_id": item.constraint_id,
                "kind": item.kind.value,
                "value": item.value,
                "strength": item.strength.value,
                "priority": item.priority,
            }
            for item in (
                *context.confirmed_hard_constraints,
                *context.confirmed_soft_constraints,
            )
        ],
        "queries": [item.model_dump(mode="json") for item in queries],
        "provider_candidates": [
            {
                "candidate_id": item.candidate.candidate_id,
                "name": item.candidate.name,
                "district": item.candidate.district,
                "area_name": item.candidate.area_name,
                "tags": list(item.candidate.tags),
                "query_ids": list(item.query_ids),
            }
            for item in observations
        ],
        "known_boundaries": {
            "prices_in_payload": False,
            "availability_verified": False,
            "booking_supported": False,
            "amenities_verified": False,
            "route_data_available": False,
            "weather_data_available": False,
            "budget_validated": False,
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class DeepSeekStayProposalModel:
    """DeepSeek adapter with separate strategy and grounded-selection schemas."""

    def __init__(self, settings: Settings) -> None:
        try:
            api_key = require_secret(settings.deepseek_api_key, "DEEPSEEK_API_KEY")
        except RuntimeError as error:
            raise StayAgentConfigurationError(str(error)) from error
        client = OpenAI(
            api_key=api_key,
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._client = wrap_openai(client, chat_name="DeepSeekStayAgent")
        self._model = settings.deepseek_model

    def _call_tool(
        self,
        *,
        system_prompt: str,
        payload: str,
        schema: ChatCompletionToolParam,
        tool_name: str,
        max_tokens: int,
    ) -> tuple[str, int, ModelTokenUsage | None]:
        messages = cast(
            list[ChatCompletionMessageParam],
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload},
            ],
        )
        started = perf_counter()
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=[schema],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            temperature=0,
            max_tokens=max_tokens,
            extra_body={"thinking": {"type": "disabled"}},
        )
        latency_ms = round((perf_counter() - started) * 1000)
        tool_calls = response.choices[0].message.tool_calls
        if tool_calls is None or len(tool_calls) != 1:
            raise StayAgentProtocolError(f"DeepSeek must return exactly one {tool_name} tool call")
        tool_call = tool_calls[0]
        if tool_call.type != "function" or tool_call.function.name != tool_name:
            raise StayAgentProtocolError("DeepSeek returned an unexpected Stay tool call")
        usage = None
        if response.usage is not None:
            usage = ModelTokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )
        return tool_call.function.arguments, latency_ms, usage

    def propose_queries(self, context: PlannerContext) -> StayQueryModelResponse:
        arguments, latency_ms, usage = self._call_tool(
            system_prompt=QUERY_SYSTEM_PROMPT,
            payload=_query_input_payload(context),
            schema=STAY_QUERY_TOOL_SCHEMA,
            tool_name=STAY_QUERY_TOOL_NAME,
            max_tokens=900,
        )
        try:
            proposal = StayQueryProposalBatch.model_validate_json(arguments)
        except (ValidationError, TypeError) as error:
            raise StayAgentProtocolError(
                "DeepSeek returned invalid Stay query arguments"
            ) from error
        return StayQueryModelResponse(
            proposal=proposal,
            model=self._model,
            latency_ms=latency_ms,
            usage=usage,
        )

    def select_candidates(
        self,
        context: PlannerContext,
        queries: tuple[StaySearchQuery, ...],
        observations: tuple[StayCandidateObservation, ...],
    ) -> StaySelectionModelResponse:
        arguments, latency_ms, usage = self._call_tool(
            system_prompt=SELECTION_SYSTEM_PROMPT,
            payload=_selection_input_payload(context, queries, observations),
            schema=STAY_SELECTION_TOOL_SCHEMA,
            tool_name=STAY_SELECTION_TOOL_NAME,
            max_tokens=1200,
        )
        try:
            proposal = StaySelectionProposalBatch.model_validate_json(arguments)
        except (ValidationError, TypeError) as error:
            raise StayAgentProtocolError(
                "DeepSeek returned invalid Stay selection arguments"
            ) from error
        return StaySelectionModelResponse(
            proposal=proposal,
            model=self._model,
            latency_ms=latency_ms,
            usage=usage,
        )


def require_stay_context_ready(context: PlannerContext) -> None:
    if PlannerCapability.STAY_SEARCH not in context.ready_capabilities:
        raise StayAgentProtocolError("stay search is blocked for this PlannerContext")
    if context.destination.administrative_code is None:
        raise StayAgentProtocolError("Stay Agent requires a supported destination adcode")
    if context.party.rooms is None or context.party.room_nights is None:
        raise StayAgentProtocolError("Stay Agent requires confirmed room count")


def normalize_stay_queries(
    context: PlannerContext,
    response: StayQueryModelResponse,
) -> tuple[StaySearchQuery, ...]:
    require_stay_context_ready(context)
    allowed_refs = set(allowed_stay_context_refs(context))
    semantic_keys: set[tuple[str, str]] = set()
    queries: list[StaySearchQuery] = []
    for item in response.proposal.items:
        semantic_key = (item.target_area.casefold(), item.keywords.casefold())
        if semantic_key in semantic_keys:
            raise StayAgentProtocolError("Stay query proposal contains a duplicate query")
        semantic_keys.add(semantic_key)
        if not set(item.context_refs).issubset(allowed_refs):
            raise StayAgentProtocolError("Stay query references unknown context evidence")
        material = f"{context.context_id}|{item.target_area.casefold()}|{item.keywords.casefold()}"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
        queries.append(
            StaySearchQuery(
                query_id=f"stay-query-{digest}",
                target_area=item.target_area,
                keywords=item.keywords,
                reason=item.reason,
                context_refs=item.context_refs,
            )
        )
    return tuple(queries)


async def search_stay_candidates(
    context: PlannerContext,
    queries: tuple[StaySearchQuery, ...],
    provider: StaySearchProvider,
) -> tuple[StayCandidateObservation, ...]:
    city_adcode = context.destination.administrative_code
    if city_adcode is None:
        raise StayAgentProtocolError("Stay Agent requires a supported destination adcode")
    candidates_by_id: dict[str, CandidateStay] = {}
    query_ids_by_candidate: dict[str, list[str]] = {}
    for query in queries:
        candidates = await provider.search_stays(
            StaySearchRequest(
                keywords=query.keywords,
                city_adcode=city_adcode,
                limit=MAX_CANDIDATES_PER_QUERY,
            )
        )
        if len(candidates) > MAX_CANDIDATES_PER_QUERY:
            raise StayAgentProtocolError("provider returned more stay candidates than requested")
        response_ids = [item.candidate_id for item in candidates]
        if len(response_ids) != len(set(response_ids)):
            raise StayAgentProtocolError("provider returned duplicate stay candidate ids")
        for candidate in candidates:
            if candidate.city != context.destination.normalized_name:
                raise StayAgentProtocolError("provider stay candidate city does not match context")
            existing = candidates_by_id.get(candidate.candidate_id)
            if existing is not None and existing != candidate:
                raise StayAgentProtocolError(
                    "provider reused a stay candidate id for different candidate facts"
                )
            if existing is None:
                candidates_by_id[candidate.candidate_id] = candidate
                query_ids_by_candidate[candidate.candidate_id] = []
            query_ids_by_candidate[candidate.candidate_id].append(query.query_id)
    if not candidates_by_id:
        raise StayAgentProtocolError("Stay provider searches returned no candidates")
    return tuple(
        StayCandidateObservation(
            candidate=candidate,
            query_ids=tuple(query_ids_by_candidate[candidate_id]),
        )
        for candidate_id, candidate in candidates_by_id.items()
    )


def _validate_evidence(
    proposal: StayCandidateSelectionProposal,
    observation: StayCandidateObservation,
) -> None:
    candidate = observation.candidate
    allowed_values = {
        StayEvidenceKind.QUERY_MATCH: set(observation.query_ids),
        StayEvidenceKind.AREA_NAME: {candidate.area_name},
        StayEvidenceKind.DISTRICT: ({candidate.district} if candidate.district else set()),
        StayEvidenceKind.TAG: set(candidate.tags),
    }
    evidence_keys = [(item.kind, item.value) for item in proposal.evidence]
    if len(evidence_keys) != len(set(evidence_keys)):
        raise StayAgentProtocolError("Stay selection repeats an evidence reference")
    for evidence in proposal.evidence:
        if evidence.value not in allowed_values[evidence.kind]:
            raise StayAgentProtocolError(
                "Stay selection evidence is not present in provider candidate facts"
            )


def normalize_stay_selection(
    context: PlannerContext,
    queries: tuple[StaySearchQuery, ...],
    observations: tuple[StayCandidateObservation, ...],
    query_response: StayQueryModelResponse,
    selection_response: StaySelectionModelResponse,
) -> StayAgentResult:
    observations_by_id = {item.candidate.candidate_id: item for item in observations}
    proposal_ids = [item.candidate_id for item in selection_response.proposal.items]
    if len(proposal_ids) != len(set(proposal_ids)):
        raise StayAgentProtocolError("Stay selection repeats a candidate id")
    if not set(proposal_ids).issubset(observations_by_id):
        raise StayAgentProtocolError("Stay selection references an unknown candidate id")
    ordered = sorted(selection_response.proposal.items, key=lambda item: item.rank)
    if [item.rank for item in ordered] != list(range(1, len(ordered) + 1)):
        raise StayAgentProtocolError("Stay selection ranks must be contiguous from one")
    recommendations: list[StayRecommendation] = []
    for proposal in ordered:
        observation = observations_by_id[proposal.candidate_id]
        _validate_evidence(proposal, observation)
        recommendations.append(
            StayRecommendation(
                proposal=proposal,
                candidate=observation.candidate,
                query_ids=observation.query_ids,
            )
        )
    return StayAgentResult(
        request_id=context.request_id,
        context_id=context.context_id,
        candidate_set_sha256=stay_candidate_set_sha256(
            tuple(item.candidate for item in observations)
        ),
        queries=queries,
        observations=observations,
        recommendations=tuple(recommendations),
        query_model=query_response.model,
        selection_model=selection_response.model,
        query_latency_ms=query_response.latency_ms,
        selection_latency_ms=selection_response.latency_ms,
        query_usage=query_response.usage,
        selection_usage=selection_response.usage,
    )


def build_stay_agent_graph(
    model: StayProposalModel,
    provider: StaySearchProvider,
) -> CompiledStateGraph[
    StayAgentState,
    None,
    StayAgentState,
    StayAgentState,
]:
    def propose_queries(state: StayAgentState) -> Mapping[str, object]:
        require_stay_context_ready(state["context"])
        response = model.propose_queries(state["context"])
        return {
            "query_response": response,
            "queries": normalize_stay_queries(state["context"], response),
        }

    async def search_candidates(state: StayAgentState) -> Mapping[str, object]:
        queries = state.get("queries")
        if queries is None:
            raise StayAgentProtocolError("Stay search received no normalized queries")
        return {"observations": await search_stay_candidates(state["context"], queries, provider)}

    def select_candidates(state: StayAgentState) -> Mapping[str, object]:
        queries = state.get("queries")
        observations = state.get("observations")
        if queries is None or observations is None:
            raise StayAgentProtocolError("Stay selection received incomplete inputs")
        return {
            "selection_response": model.select_candidates(state["context"], queries, observations)
        }

    def validate_selection(state: StayAgentState) -> Mapping[str, object]:
        query_response = state.get("query_response")
        queries = state.get("queries")
        observations = state.get("observations")
        selection_response = state.get("selection_response")
        if any(
            item is None for item in (query_response, queries, observations, selection_response)
        ):
            raise StayAgentProtocolError("Stay normalizer received incomplete state")
        assert query_response is not None
        assert queries is not None
        assert observations is not None
        assert selection_response is not None
        return {
            "result": normalize_stay_selection(
                state["context"],
                queries,
                observations,
                query_response,
                selection_response,
            )
        }

    workflow = StateGraph(StayAgentState)
    workflow.add_node("propose_queries", propose_queries)
    workflow.add_node("search_candidates", search_candidates)
    workflow.add_node("select_candidates", select_candidates)
    workflow.add_node("validate_selection", validate_selection)
    workflow.add_edge(START, "propose_queries")
    workflow.add_edge("propose_queries", "search_candidates")
    workflow.add_edge("search_candidates", "select_candidates")
    workflow.add_edge("select_candidates", "validate_selection")
    workflow.add_edge("validate_selection", END)
    return workflow.compile(checkpointer=False, name=STAY_AGENT_NAME)


def build_stay_run_config(context: PlannerContext, *, model: str) -> RunnableConfig:
    return {
        "run_name": STAY_AGENT_NAME,
        "tags": ["ez-203", "stay-agent", "schema-constrained", "provider-grounded"],
        "metadata": {
            "agent_version": "stay-agent-v1",
            "query_prompt_version": STAY_QUERY_PROMPT_VERSION,
            "selection_prompt_version": STAY_SELECTION_PROMPT_VERSION,
            "request_id": context.request_id,
            "context_id": context.context_id,
            "model": model,
            "raw_user_text_in_metadata": False,
            "hotel_price_claims_enabled": False,
            "booking_enabled": False,
        },
    }


async def run_stay_agent(
    context: PlannerContext,
    provider: StaySearchProvider,
    model: StayProposalModel,
) -> StayAgentResult:
    require_stay_context_ready(context)
    graph = build_stay_agent_graph(model, provider)
    final_state = cast(
        StayAgentState,
        await graph.ainvoke(
            {"context": context},
            config=build_stay_run_config(context, model="injected-model"),
        ),
    )
    result = final_state.get("result")
    if result is None:
        raise StayAgentProtocolError("Stay Agent completed without a result")
    return result


async def run_live_stay_agent(
    context: PlannerContext,
    provider: StaySearchProvider,
    settings: Settings,
) -> StayAgentResult:
    require_stay_context_ready(context)
    if not settings.langsmith_tracing:
        raise StayAgentConfigurationError("LANGSMITH_TRACING must be true for the live Stay Agent")
    redactor = TraceRedactor.from_settings(settings)
    try:
        langsmith_client = build_langsmith_client(settings, redactor)
    except RuntimeError as error:
        raise StayAgentConfigurationError(str(error)) from error
    model = DeepSeekStayProposalModel(settings)
    graph = build_stay_agent_graph(model, provider)
    try:
        with tracing_context(
            enabled=True,
            client=langsmith_client,
            project_name=settings.langsmith_project,
        ):
            final_state = cast(
                StayAgentState,
                await graph.ainvoke(
                    {"context": context},
                    config=build_stay_run_config(context, model=settings.deepseek_model),
                ),
            )
    finally:
        langsmith_client.flush(timeout=15.0)
    result = final_state.get("result")
    if result is None:
        raise StayAgentProtocolError("live Stay Agent completed without a result")
    return result
