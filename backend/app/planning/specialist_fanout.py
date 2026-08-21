import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal, NotRequired, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot
from langsmith import tracing_context
from pydantic import ValidationError

from app.agents.contracts import (
    ExploreCandidateObservation,
    ExploreQueryModelResponse,
    ExploreSearchQuery,
    ExploreSelectionModelResponse,
    ModelTokenUsage,
    StayCandidateObservation,
    StayQueryModelResponse,
    StaySearchQuery,
    StaySelectionModelResponse,
)
from app.agents.explore_agent import (
    DeepSeekExploreProposalModel,
    ExploreAgentProtocolError,
    ExploreProposalModel,
    run_explore_agent,
)
from app.agents.stay_agent import (
    DeepSeekStayProposalModel,
    StayAgentProtocolError,
    StayProposalModel,
    run_stay_agent,
)
from app.core.config import Settings
from app.domain.candidates import CandidatePOI, CandidateStay
from app.domain.context import PlannerCapability, PlannerContext
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.travel_data import WeatherRisk
from app.observability.probe import build_langsmith_client
from app.observability.redaction import TraceRedactor
from app.planning.context_compiler import compile_planner_context
from app.planning.specialist_contracts import (
    SpecialistBranchResult,
    SpecialistBranchStatus,
    SpecialistFailure,
    SpecialistFailureCategory,
    SpecialistFanoutResult,
    SpecialistFanoutSnapshot,
    SpecialistFanoutStatus,
    SpecialistName,
    SpecialistSkipReason,
)
from app.providers.errors import ProviderRequestError
from app.providers.ports import (
    POISearchRequest,
    SpecialistProvider,
    StaySearchRequest,
    WeatherRiskRequest,
)

SPECIALIST_FANOUT_GRAPH_NAME = "eztrip-specialist-fanout-v1"


class SpecialistFanoutConfigurationError(RuntimeError):
    """Raised when live fan-out observability or model dependencies are missing."""


class SpecialistFanoutProtocolError(RuntimeError):
    """Raised when fan-out state or checkpoint data violates the merge contract."""


class DuplicateSpecialistThreadError(SpecialistFanoutProtocolError):
    """Raised when a caller tries to replace an existing specialist checkpoint."""


def append_branch_results(
    current: tuple[dict[str, object], ...],
    update: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    return (*current, *update)


class SpecialistFanoutGraphState(TypedDict):
    request: dict[str, object]
    data_mode: str
    branch_results: Annotated[tuple[dict[str, object], ...], append_branch_results]
    planner_context: NotRequired[dict[str, object]]
    fanout_started_epoch_ms: NotRequired[int]
    result: NotRequired[dict[str, object]]


class _CountingExploreModel:
    def __init__(self, inner: ExploreProposalModel) -> None:
        self.inner = inner
        self.calls = 0
        self.usages: list[ModelTokenUsage] = []

    def propose_queries(self, context: PlannerContext) -> ExploreQueryModelResponse:
        self.calls += 1
        response = self.inner.propose_queries(context)
        if response.usage is not None:
            self.usages.append(response.usage)
        return response

    def select_candidates(
        self,
        context: PlannerContext,
        queries: tuple[ExploreSearchQuery, ...],
        observations: tuple[ExploreCandidateObservation, ...],
    ) -> ExploreSelectionModelResponse:
        self.calls += 1
        response = self.inner.select_candidates(context, queries, observations)
        if response.usage is not None:
            self.usages.append(response.usage)
        return response


class _CountingStayModel:
    def __init__(self, inner: StayProposalModel) -> None:
        self.inner = inner
        self.calls = 0
        self.usages: list[ModelTokenUsage] = []

    def propose_queries(self, context: PlannerContext) -> StayQueryModelResponse:
        self.calls += 1
        response = self.inner.propose_queries(context)
        if response.usage is not None:
            self.usages.append(response.usage)
        return response

    def select_candidates(
        self,
        context: PlannerContext,
        queries: tuple[StaySearchQuery, ...],
        observations: tuple[StayCandidateObservation, ...],
    ) -> StaySelectionModelResponse:
        self.calls += 1
        response = self.inner.select_candidates(context, queries, observations)
        if response.usage is not None:
            self.usages.append(response.usage)
        return response


class _CountingPOIProvider:
    def __init__(self, inner: SpecialistProvider) -> None:
        self.inner = inner
        self.calls = 0

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        self.calls += 1
        return await self.inner.search_pois(request)


class _CountingStayProvider:
    def __init__(self, inner: SpecialistProvider) -> None:
        self.inner = inner
        self.calls = 0

    async def search_stays(self, request: StaySearchRequest) -> tuple[CandidateStay, ...]:
        self.calls += 1
        return await self.inner.search_stays(request)


class _CountingWeatherProvider:
    def __init__(self, inner: SpecialistProvider) -> None:
        self.inner = inner
        self.calls = 0

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        self.calls += 1
        return await self.inner.get_weather_risks(request)


def _require_request(state: SpecialistFanoutGraphState) -> TripRequest:
    try:
        return TripRequest.model_validate(state["request"])
    except (KeyError, ValidationError) as error:
        raise SpecialistFanoutProtocolError(
            "specialist fan-out received no valid request"
        ) from error


def _require_context(state: SpecialistFanoutGraphState) -> PlannerContext:
    raw_context = state.get("planner_context")
    if raw_context is None:
        raise SpecialistFanoutProtocolError("specialist branch received no PlannerContext")
    try:
        return PlannerContext.model_validate(raw_context)
    except ValidationError as error:
        raise SpecialistFanoutProtocolError("specialist branch received invalid context") from error


def _require_data_mode(state: SpecialistFanoutGraphState) -> DataMode:
    try:
        data_mode = DataMode(state["data_mode"])
    except (KeyError, ValueError) as error:
        raise SpecialistFanoutProtocolError(
            "specialist fan-out received invalid data mode"
        ) from error
    if data_mode not in {DataMode.LIVE, DataMode.FIXTURE}:
        raise SpecialistFanoutProtocolError("specialist fan-out requires live or fixture data")
    return data_mode


def _elapsed_ms(started: float) -> int:
    return max(round((perf_counter() - started) * 1000), 0)


def _skip_result(specialist: SpecialistName) -> SpecialistBranchResult:
    return SpecialistBranchResult(
        specialist=specialist,
        status=SpecialistBranchStatus.SKIPPED,
        elapsed_ms=0,
        model_call_count=0,
        provider_call_count=0,
        skip_reason=SpecialistSkipReason.CAPABILITY_BLOCKED,
    )


def _failure_from_error(
    specialist: SpecialistName,
    error: Exception,
) -> SpecialistFailure:
    material = f"{specialist.value}|{error.__class__.__module__}|{error.__class__.__qualname__}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    if isinstance(error, ProviderRequestError):
        return SpecialistFailure(
            specialist=specialist,
            category=SpecialistFailureCategory.PROVIDER,
            error_code=f"{specialist.value}-provider-{digest}",
            retryable=error.failure.retryable,
            provider_category=error.failure.category,
        )
    if isinstance(error, (ExploreAgentProtocolError, StayAgentProtocolError)):
        return SpecialistFailure(
            specialist=specialist,
            category=SpecialistFailureCategory.PROTOCOL,
            error_code=f"{specialist.value}-protocol-{digest}",
            retryable=False,
        )
    return SpecialistFailure(
        specialist=specialist,
        category=SpecialistFailureCategory.DEPENDENCY,
        error_code=f"{specialist.value}-dependency-{digest}",
        retryable=False,
    )


def _branch_update(result: SpecialistBranchResult) -> tuple[dict[str, object], ...]:
    return (result.model_dump(mode="json"),)


def build_specialist_fanout_graph(
    provider: SpecialistProvider,
    explore_model: ExploreProposalModel,
    stay_model: StayProposalModel,
    *,
    checkpointer: BaseCheckpointSaver[str] | Literal[False] = False,
) -> CompiledStateGraph[
    SpecialistFanoutGraphState,
    None,
    SpecialistFanoutGraphState,
    SpecialistFanoutGraphState,
]:
    def compile_context_node(state: SpecialistFanoutGraphState) -> dict[str, object]:
        request = _require_request(state)
        _require_data_mode(state)
        context = compile_planner_context(request)
        return {
            "planner_context": context.model_dump(mode="json"),
            "fanout_started_epoch_ms": time.time_ns() // 1_000_000,
        }

    async def explore_node(state: SpecialistFanoutGraphState) -> dict[str, object]:
        context = _require_context(state)
        if PlannerCapability.CANDIDATE_SEARCH not in context.ready_capabilities:
            return {"branch_results": _branch_update(_skip_result(SpecialistName.EXPLORE))}
        counted_model = _CountingExploreModel(explore_model)
        counted_provider = _CountingPOIProvider(provider)
        started = perf_counter()
        try:
            result = await run_explore_agent(context, counted_provider, counted_model)
        except Exception as error:
            branch = SpecialistBranchResult(
                specialist=SpecialistName.EXPLORE,
                status=SpecialistBranchStatus.FAILED,
                elapsed_ms=_elapsed_ms(started),
                model_call_count=counted_model.calls,
                provider_call_count=counted_provider.calls,
                model_usages=tuple(counted_model.usages),
                failure=_failure_from_error(SpecialistName.EXPLORE, error),
            )
        else:
            branch = SpecialistBranchResult(
                specialist=SpecialistName.EXPLORE,
                status=SpecialistBranchStatus.SUCCEEDED,
                elapsed_ms=_elapsed_ms(started),
                model_call_count=counted_model.calls,
                provider_call_count=counted_provider.calls,
                model_usages=tuple(counted_model.usages),
                explore_result=result,
            )
        return {"branch_results": _branch_update(branch)}

    async def stay_node(state: SpecialistFanoutGraphState) -> dict[str, object]:
        context = _require_context(state)
        if PlannerCapability.STAY_SEARCH not in context.ready_capabilities:
            return {"branch_results": _branch_update(_skip_result(SpecialistName.STAY))}
        counted_model = _CountingStayModel(stay_model)
        counted_provider = _CountingStayProvider(provider)
        started = perf_counter()
        try:
            result = await run_stay_agent(context, counted_provider, counted_model)
        except Exception as error:
            branch = SpecialistBranchResult(
                specialist=SpecialistName.STAY,
                status=SpecialistBranchStatus.FAILED,
                elapsed_ms=_elapsed_ms(started),
                model_call_count=counted_model.calls,
                provider_call_count=counted_provider.calls,
                model_usages=tuple(counted_model.usages),
                failure=_failure_from_error(SpecialistName.STAY, error),
            )
        else:
            branch = SpecialistBranchResult(
                specialist=SpecialistName.STAY,
                status=SpecialistBranchStatus.SUCCEEDED,
                elapsed_ms=_elapsed_ms(started),
                model_call_count=counted_model.calls,
                provider_call_count=counted_provider.calls,
                model_usages=tuple(counted_model.usages),
                stay_result=result,
            )
        return {"branch_results": _branch_update(branch)}

    async def weather_node(state: SpecialistFanoutGraphState) -> dict[str, object]:
        context = _require_context(state)
        if PlannerCapability.WEATHER_LOOKUP not in context.ready_capabilities:
            return {"branch_results": _branch_update(_skip_result(SpecialistName.WEATHER))}
        city_adcode = context.destination.administrative_code
        if city_adcode is None:
            raise SpecialistFanoutProtocolError(
                "weather capability is ready without a destination adcode"
            )
        counted_provider = _CountingWeatherProvider(provider)
        started = perf_counter()
        try:
            risks = await counted_provider.get_weather_risks(
                WeatherRiskRequest(city_adcode=city_adcode)
            )
        except Exception as error:
            branch = SpecialistBranchResult(
                specialist=SpecialistName.WEATHER,
                status=SpecialistBranchStatus.FAILED,
                elapsed_ms=_elapsed_ms(started),
                model_call_count=0,
                provider_call_count=counted_provider.calls,
                failure=_failure_from_error(SpecialistName.WEATHER, error),
            )
        else:
            branch = SpecialistBranchResult(
                specialist=SpecialistName.WEATHER,
                status=SpecialistBranchStatus.SUCCEEDED,
                elapsed_ms=_elapsed_ms(started),
                model_call_count=0,
                provider_call_count=counted_provider.calls,
                weather_risks=risks,
            )
        return {"branch_results": _branch_update(branch)}

    def merge_node(state: SpecialistFanoutGraphState) -> dict[str, object]:
        request = _require_request(state)
        context = _require_context(state)
        data_mode = _require_data_mode(state)
        raw_branches = state.get("branch_results", ())
        try:
            branches = tuple(
                sorted(
                    (SpecialistBranchResult.model_validate(item) for item in raw_branches),
                    key=lambda item: tuple(SpecialistName).index(item.specialist),
                )
            )
        except ValidationError as error:
            raise SpecialistFanoutProtocolError(
                "fan-out merge received an invalid branch"
            ) from error
        if len(branches) != len(SpecialistName):
            raise SpecialistFanoutProtocolError(
                "fan-out merge requires exactly one result from every specialist"
            )
        successful = sum(item.status == SpecialistBranchStatus.SUCCEEDED for item in branches)
        failed = sum(item.status == SpecialistBranchStatus.FAILED for item in branches)
        status = SpecialistFanoutStatus.PARTIAL
        if successful == len(branches):
            status = SpecialistFanoutStatus.COMPLETE
        elif successful == 0 and failed == 0:
            status = SpecialistFanoutStatus.BLOCKED
        elif successful == 0:
            status = SpecialistFanoutStatus.FAILED
        started_epoch_ms = state.get("fanout_started_epoch_ms")
        if not isinstance(started_epoch_ms, int):
            raise SpecialistFanoutProtocolError("fan-out merge received no start timestamp")
        measured_latency = max((time.time_ns() // 1_000_000) - started_epoch_ms, 0)
        fanout_latency_ms = max(
            measured_latency,
            max(item.elapsed_ms for item in branches),
        )
        result = SpecialistFanoutResult(
            request_id=request.request_id,
            context_id=context.context_id,
            data_mode=data_mode,
            status=status,
            planner_context=context,
            branches=branches,
            total_model_call_count=sum(item.model_call_count for item in branches),
            total_provider_call_count=sum(item.provider_call_count for item in branches),
            fanout_latency_ms=fanout_latency_ms,
        )
        return {"result": result.model_dump(mode="json")}

    workflow = StateGraph(SpecialistFanoutGraphState)
    workflow.add_node("compile_context", compile_context_node)
    workflow.add_node(SpecialistName.EXPLORE.value, explore_node)
    workflow.add_node(SpecialistName.STAY.value, stay_node)
    workflow.add_node(SpecialistName.WEATHER.value, weather_node)
    workflow.add_node("merge_specialists", merge_node)
    workflow.add_edge(START, "compile_context")
    for specialist in SpecialistName:
        workflow.add_edge("compile_context", specialist.value)
    workflow.add_edge([item.value for item in SpecialistName], "merge_specialists")
    workflow.add_edge("merge_specialists", END)
    return workflow.compile(checkpointer=checkpointer, name=SPECIALIST_FANOUT_GRAPH_NAME)


def build_specialist_fanout_config(
    thread_id: str | None,
    *,
    request_id: str,
    explore_model: str,
    stay_model: str,
) -> RunnableConfig:
    config: RunnableConfig = {
        "run_name": SPECIALIST_FANOUT_GRAPH_NAME,
        "tags": ["ez-204", "parallel-fanout", "typed-degradation"],
        "metadata": {
            "workflow_version": "specialist-fanout-v1",
            "request_id": request_id,
            "explore_model": explore_model,
            "stay_model": stay_model,
            "weather_uses_model": False,
            "raw_user_text_in_metadata": False,
        },
    }
    if thread_id is not None:
        config["configurable"] = {"thread_id": thread_id}
    return config


async def _invoke_fanout_graph(
    graph: CompiledStateGraph[
        SpecialistFanoutGraphState,
        None,
        SpecialistFanoutGraphState,
        SpecialistFanoutGraphState,
    ],
    request: TripRequest,
    *,
    data_mode: DataMode,
    config: RunnableConfig,
) -> SpecialistFanoutResult:
    initial: SpecialistFanoutGraphState = {
        "request": request.model_dump(mode="json"),
        "data_mode": data_mode.value,
        "branch_results": (),
    }
    final_state = cast(
        SpecialistFanoutGraphState,
        await graph.ainvoke(initial, config=config),
    )
    raw_result = final_state.get("result")
    if raw_result is None:
        raise SpecialistFanoutProtocolError("specialist fan-out completed without a result")
    return SpecialistFanoutResult.model_validate(raw_result)


async def run_specialist_fanout(
    request: TripRequest,
    provider: SpecialistProvider,
    explore_model: ExploreProposalModel,
    stay_model: StayProposalModel,
    *,
    data_mode: DataMode,
) -> SpecialistFanoutResult:
    graph = build_specialist_fanout_graph(provider, explore_model, stay_model)
    return await _invoke_fanout_graph(
        graph,
        request,
        data_mode=data_mode,
        config=build_specialist_fanout_config(
            None,
            request_id=request.request_id,
            explore_model="injected-model",
            stay_model="injected-model",
        ),
    )


async def run_live_specialist_fanout(
    request: TripRequest,
    provider: SpecialistProvider,
    settings: Settings,
    *,
    data_mode: DataMode,
) -> SpecialistFanoutResult:
    if not settings.langsmith_tracing:
        raise SpecialistFanoutConfigurationError(
            "LANGSMITH_TRACING must be true for live specialist fan-out"
        )
    redactor = TraceRedactor.from_settings(settings)
    try:
        langsmith_client = build_langsmith_client(settings, redactor)
        explore_model = DeepSeekExploreProposalModel(settings)
        stay_model = DeepSeekStayProposalModel(settings)
    except RuntimeError as error:
        raise SpecialistFanoutConfigurationError(str(error)) from error
    graph = build_specialist_fanout_graph(provider, explore_model, stay_model)
    try:
        with tracing_context(
            enabled=True,
            client=langsmith_client,
            project_name=settings.langsmith_project,
        ):
            return await _invoke_fanout_graph(
                graph,
                request,
                data_mode=data_mode,
                config=build_specialist_fanout_config(
                    None,
                    request_id=request.request_id,
                    explore_model=settings.deepseek_model,
                    stay_model=settings.deepseek_model,
                ),
            )
    finally:
        langsmith_client.flush(timeout=15.0)


def _snapshot_checkpoint_id(snapshot: StateSnapshot) -> str:
    configurable = snapshot.config.get("configurable", {})
    checkpoint_id = configurable.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise SpecialistFanoutProtocolError("specialist checkpoint has no checkpoint_id")
    return checkpoint_id


class SpecialistFanoutRuntime:
    def __init__(
        self,
        provider: SpecialistProvider,
        explore_model: ExploreProposalModel,
        stay_model: StayProposalModel,
        checkpointer: BaseCheckpointSaver[str],
    ) -> None:
        self.graph = build_specialist_fanout_graph(
            provider,
            explore_model,
            stay_model,
            checkpointer=checkpointer,
        )

    async def start(
        self,
        thread_id: str,
        request: TripRequest,
        *,
        data_mode: DataMode,
    ) -> SpecialistFanoutSnapshot:
        config = build_specialist_fanout_config(
            thread_id,
            request_id=request.request_id,
            explore_model="injected-model",
            stay_model="injected-model",
        )
        existing = await self.graph.aget_state(config)
        if existing.values:
            raise DuplicateSpecialistThreadError(
                f"specialist thread {thread_id} already has checkpoint state"
            )
        await _invoke_fanout_graph(
            self.graph,
            request,
            data_mode=data_mode,
            config=config,
        )
        return await self.snapshot(thread_id)

    async def snapshot(self, thread_id: str) -> SpecialistFanoutSnapshot:
        config = build_specialist_fanout_config(
            thread_id,
            request_id="checkpoint-snapshot",
            explore_model="injected-model",
            stay_model="injected-model",
        )
        snapshot = await self.graph.aget_state(config)
        if not snapshot.values:
            raise SpecialistFanoutProtocolError(f"specialist thread {thread_id} does not exist")
        values = cast(dict[str, object], snapshot.values)
        raw_request = values.get("request")
        raw_result = values.get("result")
        if raw_request is None or raw_result is None:
            raise SpecialistFanoutProtocolError("specialist checkpoint is incomplete")
        return SpecialistFanoutSnapshot(
            thread_id=thread_id,
            checkpoint_id=_snapshot_checkpoint_id(snapshot),
            next_nodes=tuple(str(node) for node in snapshot.next),
            request=TripRequest.model_validate(raw_request),
            result=SpecialistFanoutResult.model_validate(raw_result),
        )


@asynccontextmanager
async def open_sqlite_specialist_runtime(
    checkpoint_path: Path,
    provider: SpecialistProvider,
    explore_model: ExploreProposalModel,
    stay_model: StayProposalModel,
) -> AsyncIterator[SpecialistFanoutRuntime]:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        yield SpecialistFanoutRuntime(
            provider,
            explore_model,
            stay_model,
            checkpointer,
        )
