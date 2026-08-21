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
    ExploreAgentResult,
    ExploreCandidateObservation,
    ExploreCandidateSelectionProposal,
    ExploreEvidenceKind,
    ExploreQueryModelResponse,
    ExploreQueryProposalBatch,
    ExploreRecommendation,
    ExploreSearchQuery,
    ExploreSelectionModelResponse,
    ExploreSelectionProposalBatch,
    ModelTokenUsage,
)
from app.agents.hashing import candidate_set_sha256
from app.core.config import Settings
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerCapability, PlannerContext
from app.observability.probe import build_langsmith_client, require_secret
from app.observability.redaction import TraceRedactor
from app.providers.ports import POISearchRequest, TravelDataProvider

EXPLORE_AGENT_NAME = "eztrip-explore-agent-v1"
EXPLORE_QUERY_PROMPT_VERSION = "explore-query-strategy-v1"
EXPLORE_SELECTION_PROMPT_VERSION = "explore-candidate-selection-v1"
EXPLORE_QUERY_TOOL_NAME = "submit_explore_queries"
EXPLORE_SELECTION_TOOL_NAME = "submit_explore_selection"
MAX_CANDIDATES_PER_QUERY = 3

QUERY_SYSTEM_PROMPT = """你是 EzTrip Explore Agent 的搜索策略节点。
只根据结构化 PlannerContext 设计 1 到 4 条高德 POI 文本搜索关键词, 可选择 attraction 或 dining。
搜索要覆盖已确认的必去项、兴趣、旅行风格和同行人特征; 不要自行添加用户没有表达的硬约束。
context_refs 只能复制输入 allowed_context_refs 中的完整字符串; 没有对应依据时留空。
只决定 kind、keywords、reason 和 context_refs。
不得生成景点事实、候选 ID、坐标、票价、营业时间或来源。
必须调用 submit_explore_queries, 不要输出正文。"""

SELECTION_SYSTEM_PROMPT = """你是 EzTrip Explore Agent 的候选筛选节点。
只能从输入 provider_candidates 中选择 1 到 6 个 candidate_id, 并给出从 1 开始连续的 rank。
这是相关性筛选, 不是候选全量覆盖任务; 有合适候选时也可以只选 1 个。
不得为了凑数选择与 confirmed_constraints、travel_styles 或同行人特征没有直接关系的候选。
若某候选的类别和标签明显偏离用户偏好, 必须排除, 即使它来自同一查询结果。
不得新增候选、改名或补充未提供的事实。每条推荐必须提供至少一个可核验 evidence:
query_match 只能引用候选的 query_ids; category、district、environment、tag 只能逐字复制候选字段。
reason 只解释候选与已给偏好的匹配, 不得声称知道实时票价、营业时间、排队、路线、天气或可订状态。
必须调用 submit_explore_selection, 不要输出正文。"""


class ExploreAgentConfigurationError(RuntimeError):
    """Raised when a live Explore Agent dependency is not configured."""


class ExploreAgentProtocolError(RuntimeError):
    """Raised when model or provider output violates the Explore grounding boundary."""


class ExploreProposalModel(Protocol):
    def propose_queries(self, context: PlannerContext) -> ExploreQueryModelResponse: ...

    def select_candidates(
        self,
        context: PlannerContext,
        queries: tuple[ExploreSearchQuery, ...],
        observations: tuple[ExploreCandidateObservation, ...],
    ) -> ExploreSelectionModelResponse: ...


class ExploreAgentState(TypedDict):
    context: PlannerContext
    query_response: NotRequired[ExploreQueryModelResponse]
    queries: NotRequired[tuple[ExploreSearchQuery, ...]]
    observations: NotRequired[tuple[ExploreCandidateObservation, ...]]
    selection_response: NotRequired[ExploreSelectionModelResponse]
    result: NotRequired[ExploreAgentResult]


EXPLORE_QUERY_TOOL_SCHEMA = cast(
    ChatCompletionToolParam,
    {
        "type": "function",
        "function": {
            "name": EXPLORE_QUERY_TOOL_NAME,
            "description": "提交基于结构化旅行偏好的 POI 搜索策略。",
            "parameters": ExploreQueryProposalBatch.model_json_schema(mode="validation"),
        },
    },
)

EXPLORE_SELECTION_TOOL_SCHEMA = cast(
    ChatCompletionToolParam,
    {
        "type": "function",
        "function": {
            "name": EXPLORE_SELECTION_TOOL_NAME,
            "description": "提交对 Provider 候选的可追溯筛选与排序。",
            "parameters": ExploreSelectionProposalBatch.model_json_schema(mode="validation"),
        },
    },
)


def allowed_context_refs(context: PlannerContext) -> tuple[str, ...]:
    constraint_refs = tuple(
        f"constraint:{item.constraint_id}"
        for item in (
            *context.confirmed_hard_constraints,
            *context.confirmed_soft_constraints,
        )
    )
    style_refs = tuple(f"travel_style:{item}" for item in context.travel_styles)
    return (*constraint_refs, *style_refs)


def _query_input_payload(context: PlannerContext) -> str:
    constraints = (
        *context.confirmed_hard_constraints,
        *context.confirmed_soft_constraints,
    )
    payload = {
        "destination": {
            "name": context.destination.normalized_name,
            "administrative_code": context.destination.administrative_code,
        },
        "trip": {
            "day_count": context.day_count,
            "party": {
                "adults": context.party.adults,
                "children": context.party.children,
                "seniors": context.party.seniors,
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
            for item in constraints
        ],
        "allowed_context_refs": list(allowed_context_refs(context)),
        "known_boundaries": {
            "provider_facts_not_yet_available": True,
            "hotel_search_in_scope": False,
            "weather_lookup_in_scope": False,
            "route_and_budget_validation_in_scope": False,
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _selection_input_payload(
    context: PlannerContext,
    queries: tuple[ExploreSearchQuery, ...],
    observations: tuple[ExploreCandidateObservation, ...],
) -> str:
    payload = {
        "destination": context.destination.normalized_name,
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
                "categories": list(item.candidate.categories),
                "environment": item.candidate.environment.value,
                "tags": list(item.candidate.tags),
                "query_ids": list(item.query_ids),
            }
            for item in observations
        ],
        "known_boundaries": {
            "opening_hours_verified": False,
            "ticket_price_verified": False,
            "route_data_available": False,
            "weather_data_available": False,
            "budget_validated": False,
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class DeepSeekExploreProposalModel:
    """DeepSeek adapter with separate query and grounded-selection schemas."""

    def __init__(self, settings: Settings) -> None:
        try:
            api_key = require_secret(settings.deepseek_api_key, "DEEPSEEK_API_KEY")
        except RuntimeError as error:
            raise ExploreAgentConfigurationError(str(error)) from error
        client = OpenAI(
            api_key=api_key,
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._client = wrap_openai(client, chat_name="DeepSeekExploreAgent")
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
            raise ExploreAgentProtocolError(
                f"DeepSeek must return exactly one {tool_name} tool call"
            )
        tool_call = tool_calls[0]
        if tool_call.type != "function" or tool_call.function.name != tool_name:
            raise ExploreAgentProtocolError("DeepSeek returned an unexpected Explore tool call")
        usage = None
        if response.usage is not None:
            usage = ModelTokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )
        return tool_call.function.arguments, latency_ms, usage

    def propose_queries(self, context: PlannerContext) -> ExploreQueryModelResponse:
        arguments, latency_ms, usage = self._call_tool(
            system_prompt=QUERY_SYSTEM_PROMPT,
            payload=_query_input_payload(context),
            schema=EXPLORE_QUERY_TOOL_SCHEMA,
            tool_name=EXPLORE_QUERY_TOOL_NAME,
            max_tokens=900,
        )
        try:
            proposal = ExploreQueryProposalBatch.model_validate_json(arguments)
        except (ValidationError, TypeError) as error:
            raise ExploreAgentProtocolError(
                "DeepSeek returned invalid Explore query arguments"
            ) from error
        return ExploreQueryModelResponse(
            proposal=proposal,
            model=self._model,
            latency_ms=latency_ms,
            usage=usage,
        )

    def select_candidates(
        self,
        context: PlannerContext,
        queries: tuple[ExploreSearchQuery, ...],
        observations: tuple[ExploreCandidateObservation, ...],
    ) -> ExploreSelectionModelResponse:
        arguments, latency_ms, usage = self._call_tool(
            system_prompt=SELECTION_SYSTEM_PROMPT,
            payload=_selection_input_payload(context, queries, observations),
            schema=EXPLORE_SELECTION_TOOL_SCHEMA,
            tool_name=EXPLORE_SELECTION_TOOL_NAME,
            max_tokens=1200,
        )
        try:
            proposal = ExploreSelectionProposalBatch.model_validate_json(arguments)
        except (ValidationError, TypeError) as error:
            raise ExploreAgentProtocolError(
                "DeepSeek returned invalid Explore selection arguments"
            ) from error
        return ExploreSelectionModelResponse(
            proposal=proposal,
            model=self._model,
            latency_ms=latency_ms,
            usage=usage,
        )


def normalize_explore_queries(
    context: PlannerContext,
    response: ExploreQueryModelResponse,
) -> tuple[ExploreSearchQuery, ...]:
    if PlannerCapability.CANDIDATE_SEARCH not in context.ready_capabilities:
        raise ExploreAgentProtocolError("candidate search is blocked for this PlannerContext")
    if context.destination.administrative_code is None:
        raise ExploreAgentProtocolError("Explore Agent requires a supported destination adcode")
    allowed_refs = set(allowed_context_refs(context))
    semantic_keys: set[tuple[str, str]] = set()
    queries: list[ExploreSearchQuery] = []
    for item in response.proposal.items:
        semantic_key = (item.kind.value, item.keywords.casefold())
        if semantic_key in semantic_keys:
            raise ExploreAgentProtocolError("Explore query proposal contains a duplicate query")
        semantic_keys.add(semantic_key)
        if not set(item.context_refs).issubset(allowed_refs):
            raise ExploreAgentProtocolError("Explore query references unknown context evidence")
        material = f"{context.context_id}|{item.kind.value}|{item.keywords.casefold()}"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
        queries.append(
            ExploreSearchQuery(
                query_id=f"explore-query-{digest}",
                kind=item.kind,
                keywords=item.keywords,
                reason=item.reason,
                context_refs=item.context_refs,
            )
        )
    return tuple(queries)


async def search_explore_candidates(
    context: PlannerContext,
    queries: tuple[ExploreSearchQuery, ...],
    provider: TravelDataProvider,
) -> tuple[ExploreCandidateObservation, ...]:
    city_adcode = context.destination.administrative_code
    if city_adcode is None:
        raise ExploreAgentProtocolError("Explore Agent requires a supported destination adcode")
    candidates_by_id: dict[str, CandidatePOI] = {}
    query_ids_by_candidate: dict[str, list[str]] = {}
    for query in queries:
        candidates = await provider.search_pois(
            POISearchRequest(
                keywords=query.keywords,
                city_adcode=city_adcode,
                limit=MAX_CANDIDATES_PER_QUERY,
            )
        )
        if len(candidates) > MAX_CANDIDATES_PER_QUERY:
            raise ExploreAgentProtocolError("provider returned more candidates than requested")
        response_ids = [item.candidate_id for item in candidates]
        if len(response_ids) != len(set(response_ids)):
            raise ExploreAgentProtocolError("provider returned duplicate candidate ids")
        for candidate in candidates:
            if candidate.city != context.destination.normalized_name:
                raise ExploreAgentProtocolError("provider candidate city does not match context")
            existing = candidates_by_id.get(candidate.candidate_id)
            if existing is not None and existing != candidate:
                raise ExploreAgentProtocolError(
                    "provider reused a candidate id for different candidate facts"
                )
            if existing is None:
                candidates_by_id[candidate.candidate_id] = candidate
                query_ids_by_candidate[candidate.candidate_id] = []
            query_ids_by_candidate[candidate.candidate_id].append(query.query_id)
    if not candidates_by_id:
        raise ExploreAgentProtocolError("Explore provider searches returned no candidates")
    return tuple(
        ExploreCandidateObservation(
            candidate=candidate,
            query_ids=tuple(query_ids_by_candidate[candidate_id]),
        )
        for candidate_id, candidate in candidates_by_id.items()
    )


def _validate_evidence(
    proposal: ExploreCandidateSelectionProposal,
    observation: ExploreCandidateObservation,
) -> None:
    candidate = observation.candidate
    allowed_values = {
        ExploreEvidenceKind.QUERY_MATCH: set(observation.query_ids),
        ExploreEvidenceKind.CATEGORY: set(candidate.categories),
        ExploreEvidenceKind.DISTRICT: ({candidate.district} if candidate.district else set()),
        ExploreEvidenceKind.ENVIRONMENT: {candidate.environment.value},
        ExploreEvidenceKind.TAG: set(candidate.tags),
    }
    evidence_keys = [(item.kind, item.value) for item in proposal.evidence]
    if len(evidence_keys) != len(set(evidence_keys)):
        raise ExploreAgentProtocolError("Explore selection repeats an evidence reference")
    for evidence in proposal.evidence:
        if evidence.value not in allowed_values[evidence.kind]:
            raise ExploreAgentProtocolError(
                "Explore selection evidence is not present in provider candidate facts"
            )


def normalize_explore_selection(
    context: PlannerContext,
    queries: tuple[ExploreSearchQuery, ...],
    observations: tuple[ExploreCandidateObservation, ...],
    query_response: ExploreQueryModelResponse,
    selection_response: ExploreSelectionModelResponse,
) -> ExploreAgentResult:
    observations_by_id = {item.candidate.candidate_id: item for item in observations}
    proposal_ids = [item.candidate_id for item in selection_response.proposal.items]
    if len(proposal_ids) != len(set(proposal_ids)):
        raise ExploreAgentProtocolError("Explore selection repeats a candidate id")
    if not set(proposal_ids).issubset(observations_by_id):
        raise ExploreAgentProtocolError("Explore selection references an unknown candidate id")
    ordered = sorted(selection_response.proposal.items, key=lambda item: item.rank)
    if [item.rank for item in ordered] != list(range(1, len(ordered) + 1)):
        raise ExploreAgentProtocolError("Explore selection ranks must be contiguous from one")
    recommendations: list[ExploreRecommendation] = []
    for proposal in ordered:
        observation = observations_by_id[proposal.candidate_id]
        _validate_evidence(proposal, observation)
        recommendations.append(
            ExploreRecommendation(
                proposal=proposal,
                candidate=observation.candidate,
                query_ids=observation.query_ids,
            )
        )
    return ExploreAgentResult(
        request_id=context.request_id,
        context_id=context.context_id,
        candidate_set_sha256=candidate_set_sha256(tuple(item.candidate for item in observations)),
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


def build_explore_agent_graph(
    model: ExploreProposalModel,
    provider: TravelDataProvider,
) -> CompiledStateGraph[
    ExploreAgentState,
    None,
    ExploreAgentState,
    ExploreAgentState,
]:
    def propose_queries(state: ExploreAgentState) -> Mapping[str, object]:
        response = model.propose_queries(state["context"])
        return {
            "query_response": response,
            "queries": normalize_explore_queries(state["context"], response),
        }

    async def search_candidates(state: ExploreAgentState) -> Mapping[str, object]:
        queries = state.get("queries")
        if queries is None:
            raise ExploreAgentProtocolError("Explore search received no normalized queries")
        return {
            "observations": await search_explore_candidates(state["context"], queries, provider)
        }

    def select_candidates(state: ExploreAgentState) -> Mapping[str, object]:
        queries = state.get("queries")
        observations = state.get("observations")
        if queries is None or observations is None:
            raise ExploreAgentProtocolError("Explore selection received incomplete inputs")
        return {
            "selection_response": model.select_candidates(state["context"], queries, observations)
        }

    def validate_selection(state: ExploreAgentState) -> Mapping[str, object]:
        query_response = state.get("query_response")
        queries = state.get("queries")
        observations = state.get("observations")
        selection_response = state.get("selection_response")
        if any(
            item is None for item in (query_response, queries, observations, selection_response)
        ):
            raise ExploreAgentProtocolError("Explore normalizer received incomplete state")
        assert query_response is not None
        assert queries is not None
        assert observations is not None
        assert selection_response is not None
        return {
            "result": normalize_explore_selection(
                state["context"],
                queries,
                observations,
                query_response,
                selection_response,
            )
        }

    workflow = StateGraph(ExploreAgentState)
    workflow.add_node("propose_queries", propose_queries)
    workflow.add_node("search_candidates", search_candidates)
    workflow.add_node("select_candidates", select_candidates)
    workflow.add_node("validate_selection", validate_selection)
    workflow.add_edge(START, "propose_queries")
    workflow.add_edge("propose_queries", "search_candidates")
    workflow.add_edge("search_candidates", "select_candidates")
    workflow.add_edge("select_candidates", "validate_selection")
    workflow.add_edge("validate_selection", END)
    return workflow.compile(checkpointer=False, name=EXPLORE_AGENT_NAME)


def build_explore_run_config(context: PlannerContext, *, model: str) -> RunnableConfig:
    return {
        "run_name": EXPLORE_AGENT_NAME,
        "tags": ["ez-202", "explore-agent", "schema-constrained", "provider-grounded"],
        "metadata": {
            "agent_version": "explore-agent-v1",
            "query_prompt_version": EXPLORE_QUERY_PROMPT_VERSION,
            "selection_prompt_version": EXPLORE_SELECTION_PROMPT_VERSION,
            "request_id": context.request_id,
            "context_id": context.context_id,
            "model": model,
            "raw_user_text_in_metadata": False,
        },
    }


async def run_explore_agent(
    context: PlannerContext,
    provider: TravelDataProvider,
    model: ExploreProposalModel,
) -> ExploreAgentResult:
    graph = build_explore_agent_graph(model, provider)
    final_state = cast(
        ExploreAgentState,
        await graph.ainvoke(
            {"context": context},
            config=build_explore_run_config(context, model="injected-model"),
        ),
    )
    result = final_state.get("result")
    if result is None:
        raise ExploreAgentProtocolError("Explore Agent completed without a result")
    return result


async def run_live_explore_agent(
    context: PlannerContext,
    provider: TravelDataProvider,
    settings: Settings,
) -> ExploreAgentResult:
    if not settings.langsmith_tracing:
        raise ExploreAgentConfigurationError(
            "LANGSMITH_TRACING must be true for the live Explore Agent"
        )
    redactor = TraceRedactor.from_settings(settings)
    try:
        langsmith_client = build_langsmith_client(settings, redactor)
    except RuntimeError as error:
        raise ExploreAgentConfigurationError(str(error)) from error
    model = DeepSeekExploreProposalModel(settings)
    graph = build_explore_agent_graph(model, provider)
    try:
        with tracing_context(
            enabled=True,
            client=langsmith_client,
            project_name=settings.langsmith_project,
        ):
            final_state = cast(
                ExploreAgentState,
                await graph.ainvoke(
                    {"context": context},
                    config=build_explore_run_config(
                        context,
                        model=settings.deepseek_model,
                    ),
                ),
            )
    finally:
        langsmith_client.flush(timeout=15.0)
    result = final_state.get("result")
    if result is None:
        raise ExploreAgentProtocolError("live Explore Agent completed without a result")
    return result
