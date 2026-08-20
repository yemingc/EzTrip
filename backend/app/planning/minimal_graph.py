import hashlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, NotRequired, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerCapability, PlannerContext
from app.domain.provider import ProviderErrorCategory, ProviderFailure
from app.domain.request import ConstraintKind, TripRequest
from app.domain.sources import DataMode
from app.domain.workflow import (
    CandidateSearchQuery,
    MinimalPlanningResult,
    PlanningNodeEvent,
    PlanningNodeName,
    PlanningNodeOutcome,
    PlanningWorkflowStatus,
)
from app.planning.context_compiler import compile_planner_context
from app.providers.errors import ProviderRequestError
from app.providers.ports import POISearchRequest, TravelDataProvider

MINIMAL_PLANNING_GRAPH_NAME = "eztrip-minimal-planning-graph-v1"
DEFAULT_POI_QUERY_ALIASES: Mapping[str, str] = MappingProxyType({"故宫": "故宫博物院"})


class PlanningGraphProtocolError(RuntimeError):
    """Raised when a graph node receives state that violates its routing contract."""


def append_events(
    current: tuple[PlanningNodeEvent, ...],
    update: tuple[PlanningNodeEvent, ...],
) -> tuple[PlanningNodeEvent, ...]:
    return (*current, *update)


class MinimalPlanningState(TypedDict):
    request: TripRequest
    planner_context: NotRequired[PlannerContext]
    status: NotRequired[PlanningWorkflowStatus]
    candidate_queries: NotRequired[tuple[CandidateSearchQuery, ...]]
    candidates: NotRequired[tuple[CandidatePOI, ...]]
    provider_failures: NotRequired[tuple[ProviderFailure, ...]]
    events: Annotated[tuple[PlanningNodeEvent, ...], append_events]


def derive_candidate_search_queries(
    context: PlannerContext,
    *,
    aliases: Mapping[str, str] = DEFAULT_POI_QUERY_ALIASES,
) -> tuple[CandidateSearchQuery, ...]:
    city_adcode = context.destination.administrative_code
    if city_adcode is None:
        raise PlanningGraphProtocolError("candidate query derivation requires a supported city")

    normalized_aliases = {key.strip().casefold(): value.strip() for key, value in aliases.items()}
    queries: list[CandidateSearchQuery] = []
    constraints = (
        *context.confirmed_hard_constraints,
        *context.confirmed_soft_constraints,
    )
    for constraint in constraints:
        if constraint.kind != ConstraintKind.MUST_VISIT or not isinstance(constraint.value, str):
            continue
        requested_value = constraint.value.strip()
        keywords = normalized_aliases.get(requested_value.casefold(), requested_value)
        digest = hashlib.sha256(
            f"{context.context_id}|{constraint.constraint_id}|{keywords}".encode()
        ).hexdigest()[:12]
        queries.append(
            CandidateSearchQuery(
                query_id=f"candidate-query-{digest}",
                keywords=keywords,
                city_adcode=city_adcode,
                source_constraint_id=constraint.constraint_id,
                requested_value=requested_value,
            )
        )
    return tuple(queries)


def _require_context(state: MinimalPlanningState) -> PlannerContext:
    context = state.get("planner_context")
    if context is None:
        raise PlanningGraphProtocolError("planning node received no compiled context")
    return context


def _empty_result_failure(query: CandidateSearchQuery) -> ProviderFailure:
    return ProviderFailure(
        provider="travel_data",
        operation="search_pois",
        category=ProviderErrorCategory.EMPTY_RESULT,
        message=f"provider returned no candidates for query {query.query_id}",
        retryable=False,
    )


def build_minimal_planning_graph(
    provider: TravelDataProvider,
    *,
    query_aliases: Mapping[str, str] = DEFAULT_POI_QUERY_ALIASES,
) -> CompiledStateGraph[
    MinimalPlanningState,
    None,
    MinimalPlanningState,
    MinimalPlanningState,
]:
    aliases = dict(query_aliases)

    def compile_context_node(state: MinimalPlanningState) -> dict[str, object]:
        context = compile_planner_context(state["request"])
        return {
            "planner_context": context,
            "events": (
                PlanningNodeEvent(
                    node=PlanningNodeName.COMPILE_CONTEXT,
                    outcome=PlanningNodeOutcome.COMPILED,
                    detail="TripRequest 已编译为确定性 PlannerContext。",
                ),
            ),
        }

    def clarification_gate_node(state: MinimalPlanningState) -> dict[str, object]:
        context = _require_context(state)
        search_is_ready = PlannerCapability.CANDIDATE_SEARCH in context.ready_capabilities
        if not search_is_ready:
            return {
                "status": PlanningWorkflowStatus.NEEDS_CLARIFICATION,
                "events": (
                    PlanningNodeEvent(
                        node=PlanningNodeName.CLARIFICATION_GATE,
                        outcome=PlanningNodeOutcome.BLOCKED,
                        detail="候选搜索能力被输入澄清项阻塞。",
                    ),
                ),
            }
        return {
            "events": (
                PlanningNodeEvent(
                    node=PlanningNodeName.CLARIFICATION_GATE,
                    outcome=PlanningNodeOutcome.ALLOWED,
                    detail="候选搜索能力已就绪, 允许继续执行。",
                ),
            )
        }

    def route_after_clarification(state: MinimalPlanningState) -> str:
        if state.get("status") == PlanningWorkflowStatus.NEEDS_CLARIFICATION:
            return END
        return PlanningNodeName.CANDIDATE_SEARCH.value

    async def candidate_search_node(state: MinimalPlanningState) -> dict[str, object]:
        context = _require_context(state)
        queries = derive_candidate_search_queries(context, aliases=aliases)
        if not queries:
            return {
                "status": PlanningWorkflowStatus.NO_CANDIDATE_QUERY,
                "candidate_queries": (),
                "candidates": (),
                "provider_failures": (),
                "events": (
                    PlanningNodeEvent(
                        node=PlanningNodeName.CANDIDATE_SEARCH,
                        outcome=PlanningNodeOutcome.SKIPPED,
                        detail="没有已确认的必去景点, 本阶段不做开放式模型推荐。",
                    ),
                ),
            }

        candidates_by_id: dict[str, CandidatePOI] = {}
        failures: list[ProviderFailure] = []
        for query in queries:
            try:
                found = await provider.search_pois(
                    POISearchRequest(
                        keywords=query.keywords,
                        city_adcode=query.city_adcode,
                        limit=query.limit,
                    )
                )
            except ProviderRequestError as error:
                failures.append(error.failure)
                break
            if not found:
                failures.append(_empty_result_failure(query))
                break
            for candidate in found:
                candidates_by_id.setdefault(candidate.candidate_id, candidate)

        candidates = tuple(candidates_by_id.values())
        if failures:
            return {
                "status": PlanningWorkflowStatus.PROVIDER_FAILED,
                "candidate_queries": queries,
                "candidates": candidates,
                "provider_failures": tuple(failures),
                "events": (
                    PlanningNodeEvent(
                        node=PlanningNodeName.CANDIDATE_SEARCH,
                        outcome=PlanningNodeOutcome.FAILED,
                        detail="候选 provider 返回 typed failure, 工作流未伪造降级结果。",
                    ),
                ),
            }
        return {
            "status": PlanningWorkflowStatus.CANDIDATES_READY,
            "candidate_queries": queries,
            "candidates": candidates,
            "provider_failures": (),
            "events": (
                PlanningNodeEvent(
                    node=PlanningNodeName.CANDIDATE_SEARCH,
                    outcome=PlanningNodeOutcome.SUCCEEDED,
                    detail="已获得带 provider 来源的必去景点候选。",
                ),
            ),
        }

    workflow = StateGraph(MinimalPlanningState)
    workflow.add_node(PlanningNodeName.COMPILE_CONTEXT.value, compile_context_node)
    workflow.add_node(PlanningNodeName.CLARIFICATION_GATE.value, clarification_gate_node)
    workflow.add_node(PlanningNodeName.CANDIDATE_SEARCH.value, candidate_search_node)
    workflow.add_edge(START, PlanningNodeName.COMPILE_CONTEXT.value)
    workflow.add_edge(
        PlanningNodeName.COMPILE_CONTEXT.value,
        PlanningNodeName.CLARIFICATION_GATE.value,
    )
    workflow.add_conditional_edges(
        PlanningNodeName.CLARIFICATION_GATE.value,
        route_after_clarification,
        {
            PlanningNodeName.CANDIDATE_SEARCH.value: PlanningNodeName.CANDIDATE_SEARCH.value,
            END: END,
        },
    )
    workflow.add_edge(PlanningNodeName.CANDIDATE_SEARCH.value, END)
    return workflow.compile(name=MINIMAL_PLANNING_GRAPH_NAME)


def build_planning_run_config(
    request: TripRequest,
    *,
    data_mode: DataMode,
) -> RunnableConfig:
    return {
        "run_name": MINIMAL_PLANNING_GRAPH_NAME,
        "tags": ["ez-007", "planning-graph", data_mode.value],
        "metadata": {
            "workflow_version": "minimal-planning-graph-v1",
            "request_schema_version": request.schema_version,
            "request_id": request.request_id,
            "data_mode": data_mode.value,
            "raw_user_text_in_metadata": False,
        },
    }


async def run_minimal_planning_graph(
    request: TripRequest,
    provider: TravelDataProvider,
    *,
    data_mode: DataMode,
    query_aliases: Mapping[str, str] = DEFAULT_POI_QUERY_ALIASES,
) -> MinimalPlanningResult:
    graph = build_minimal_planning_graph(provider, query_aliases=query_aliases)
    initial_state: MinimalPlanningState = {"request": request, "events": ()}
    final_state = cast(
        MinimalPlanningState,
        await graph.ainvoke(
            initial_state,
            config=build_planning_run_config(request, data_mode=data_mode),
        ),
    )
    context = _require_context(final_state)
    status = final_state.get("status")
    if status is None:
        raise PlanningGraphProtocolError("planning graph completed without a status")
    return MinimalPlanningResult(
        request_id=request.request_id,
        data_mode=data_mode,
        planner_context=context,
        status=status,
        candidate_queries=final_state.get("candidate_queries", ()),
        candidates=final_state.get("candidates", ()),
        provider_failures=final_state.get("provider_failures", ()),
        events=final_state["events"],
    )
