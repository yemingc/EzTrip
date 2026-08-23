import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import ClassVar, Protocol, cast

from langsmith import trace, tracing_context
from langsmith.wrappers import wrap_openai
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import ValidationError

from app.agents.contracts import (
    ExploreAgentResult,
    ExploreCandidateObservation,
    ExploreQueryModelResponse,
    ExploreRecommendation,
    ExploreSearchQuery,
    ExploreSelectionModelResponse,
    ModelTokenUsage,
    PlannerModelResponse,
    StayAgentResult,
    StayCandidateObservation,
    StayQueryModelResponse,
    StayRecommendation,
    StaySearchQuery,
    StaySelectionModelResponse,
)
from app.agents.explore_agent import (
    DeepSeekExploreProposalModel,
    ExploreProposalModel,
    run_explore_agent,
)
from app.agents.plan_agent import DeepSeekPlanProposalModel, PlanProposalModel, run_plan_agent
from app.agents.plan_agent_contracts import PlanAgentRunResult, PlanAgentRunStatus
from app.agents.stay_agent import DeepSeekStayProposalModel, StayProposalModel, run_stay_agent
from app.core.config import Settings
from app.domain.context import PlannerContext
from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.validation import (
    IssueSeverity,
    PlanValidationReport,
    RepairAction,
    ValidationIssue,
)
from app.evaluation.comparison_contracts import COMPARISON_ARMS, ComparisonArm
from app.evaluation.comparison_run_contracts import ComparisonToolSnapshot
from app.evaluation.comparison_runner import (
    build_clean_comparison_opening_hours,
    build_comparison_tool_snapshot,
)
from app.evaluation.explore import load_explore_agent_suite
from app.evaluation.live_comparison import (
    live_comparison_pilot_dataset_sha256,
    load_live_comparison_pilot_suite,
)
from app.evaluation.live_comparison_contracts import (
    LiveComparisonCallBudget,
    LiveComparisonPilotCase,
)
from app.evaluation.live_comparison_run_contracts import (
    LiveArmTrialResult,
    LiveCallOwner,
    LiveCallPhase,
    LiveComparisonArmSummary,
    LiveComparisonPairedDelta,
    LiveComparisonPilotReport,
    LiveComparisonRunJournal,
    LiveComparisonTrialResult,
    LiveExecutionMode,
    LiveModelCallRecord,
    LiveModelCallStatus,
    LiveModelNode,
    LiveRunJournalStatus,
    LiveTrialOutcome,
    SingleSelectionModelResponse,
    SingleSelectionProposal,
    summarize_rate,
)
from app.evaluation.plan_agent import (
    PlanAgentFixtureModel,
    build_plan_agent_materials,
    load_plan_agent_suite,
)
from app.evaluation.plan_agent_contracts import PlanAgentEvalCase
from app.evaluation.planning_materials import PlanningMaterialRouteProvider
from app.evaluation.planning_materials_contracts import RouteFailureInjection
from app.evaluation.specialist_fanout import SpecialistScenarioProvider
from app.evaluation.stay import load_stay_agent_suite
from app.observability.probe import build_langsmith_client, require_secret
from app.observability.redaction import TraceRedactor
from app.planning.hard_validator import validate_hard_trip_plan
from app.planning.material_builder import build_planning_material_bundle
from app.planning.material_contracts import PlanningMaterialBundle, RouteEdgeStatus
from app.planning.product_repair import ProductRepairExecutor, ProductRepairPipeline
from app.planning.repair_contracts import (
    RepairExecutionResult,
    RepairExecutionStatus,
    RepairOutcome,
    RepairRouterResult,
)
from app.planning.repair_router import run_repair_router
from app.planning.specialist_contracts import (
    SpecialistBranchResult,
    SpecialistFanoutResult,
    SpecialistName,
)
from app.planning.specialist_fanout import run_specialist_fanout

SINGLE_SELECTION_TOOL_NAME = "select_single_agent_candidates"
SINGLE_SELECTION_PROMPT_VERSION = "single-agent-candidate-selection-v1"
SINGLE_SELECTION_TOOL_SCHEMA = cast(
    ChatCompletionToolParam,
    {
        "type": "function",
        "function": {
            "name": SINGLE_SELECTION_TOOL_NAME,
            "description": "从冻结 Provider 候选中选择景点和一个住宿锚点。",
            "parameters": SingleSelectionProposal.model_json_schema(mode="validation"),
        },
    },
)
SINGLE_SELECTION_SYSTEM_PROMPT = """你是 EzTrip 公平对照中的完整 Single Agent。
只能从输入候选 ID 中选择, 不得创造地点、价格、库存、营业时间或路线事实。
优先满足已确认的硬约束, 再覆盖旅行风格; 排除明显不相关候选。
选择 1 至 3 个 POI 和恰好 1 个住宿锚点, 并调用指定工具返回结构化结果。"""

CALL_COMPLETION_CEILINGS = {
    LiveModelNode.SINGLE_SELECTION: 900,
    LiveModelNode.SINGLE_PLAN: 900,
    LiveModelNode.EXPLORE_QUERY: 900,
    LiveModelNode.EXPLORE_SELECTION: 1200,
    LiveModelNode.STAY_QUERY: 900,
    LiveModelNode.STAY_SELECTION: 1200,
    LiveModelNode.PRODUCT_PLAN: 900,
}


class LiveComparisonRunError(RuntimeError):
    """Raised when the live pilot cannot preserve its frozen execution contract."""


class LiveCallBudgetExceeded(LiveComparisonRunError):
    """Raised before a model call that would exceed a frozen budget."""


class SingleSelectionModel(Protocol):
    def select(self, snapshot: ComparisonToolSnapshot) -> SingleSelectionModelResponse: ...


class LivePilotModelFactory(Protocol):
    def single_selection(self) -> SingleSelectionModel: ...

    def explore(self) -> ExploreProposalModel: ...

    def stay(self) -> StayProposalModel: ...

    def plan(self) -> PlanProposalModel: ...


class TrialTracer(Protocol):
    def trial(
        self,
        case: LiveComparisonPilotCase,
        repetition: int,
        trial_id: str,
        dataset_sha256: str,
    ) -> AbstractContextManager[str | None]: ...

    def flush(self) -> None: ...


def _sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_error_code(error: BaseException, prefix: str = "live-comparison-error") -> str:
    material = f"{error.__class__.__module__}|{error.__class__.__qualname__}"
    return f"{prefix}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def _elapsed_ms(started: float) -> int:
    return max(round((perf_counter() - started) * 1000), 0)


class LiveCallBudgetGuard:
    def __init__(
        self,
        budget: LiveComparisonCallBudget,
        model: str,
        on_change: Callable[[tuple[LiveModelCallRecord, ...]], None] | None = None,
    ) -> None:
        self._budget = budget
        self._model = model
        self._on_change = on_change
        self._records: dict[int, LiveModelCallRecord] = {}
        self._lock = Lock()

    @property
    def records(self) -> tuple[LiveModelCallRecord, ...]:
        with self._lock:
            return tuple(self._records[index] for index in sorted(self._records))

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change(self.records)

    def begin(
        self,
        *,
        trial_id: str,
        case_id: str,
        repetition: int,
        owner: LiveCallOwner,
        phase: LiveCallPhase,
        node: LiveModelNode,
        max_completion_tokens: int | None = None,
    ) -> int:
        ceiling = max_completion_tokens or CALL_COMPLETION_CEILINGS[node]
        with self._lock:
            records = tuple(self._records.values())
            if len(records) >= self._budget.max_model_calls:
                raise LiveCallBudgetExceeded("live pilot physical model-call budget exhausted")
            reservation = sum(item.max_completion_tokens for item in records)
            if reservation + ceiling > self._budget.max_completion_tokens:
                raise LiveCallBudgetExceeded("live pilot completion-token reservation exhausted")
            same_trial = tuple(item for item in records if item.trial_id == trial_id)
            if phase == LiveCallPhase.REPAIR:
                repair_count = sum(item.phase == LiveCallPhase.REPAIR for item in same_trial)
                if repair_count >= self._budget.repair_model_call_allowance_per_trial:
                    raise LiveCallBudgetExceeded("trial repair model-call allowance exhausted")
            elif owner == LiveCallOwner.SINGLE_AGENT:
                base_count = sum(
                    item.phase == LiveCallPhase.BASE and item.owner == LiveCallOwner.SINGLE_AGENT
                    for item in same_trial
                )
                if base_count >= (
                    self._budget.single_selection_calls_per_trial
                    + self._budget.single_plan_calls_per_trial
                ):
                    raise LiveCallBudgetExceeded("trial Single base model-call allowance exhausted")
            elif owner == LiveCallOwner.PRODUCT_SHARED_INITIAL:
                product_count = sum(
                    item.phase == LiveCallPhase.BASE
                    and item.owner == LiveCallOwner.PRODUCT_SHARED_INITIAL
                    for item in same_trial
                )
                if product_count >= (
                    self._budget.product_explore_calls_per_trial
                    + self._budget.product_stay_calls_per_trial
                    + self._budget.product_plan_calls_per_trial
                ):
                    raise LiveCallBudgetExceeded(
                        "trial Product base model-call allowance exhausted"
                    )
            call_index = len(records) + 1
            self._records[call_index] = LiveModelCallRecord(
                call_index=call_index,
                trial_id=trial_id,
                case_id=case_id,
                repetition=repetition,
                owner=owner,
                phase=phase,
                node=node,
                model=self._model,
                max_completion_tokens=ceiling,
                status=LiveModelCallStatus.STARTED,
            )
        self._notify()
        return call_index

    def succeed(
        self,
        call_index: int,
        *,
        latency_ms: int,
        usage: ModelTokenUsage | None,
    ) -> None:
        with self._lock:
            record = self._records[call_index]
            self._records[call_index] = record.model_copy(
                update={
                    "status": LiveModelCallStatus.SUCCEEDED,
                    "latency_ms": latency_ms,
                    "usage": usage,
                }
            )
        self._notify()

    def fail(self, call_index: int, error: BaseException, *, latency_ms: int) -> None:
        with self._lock:
            record = self._records[call_index]
            self._records[call_index] = record.model_copy(
                update={
                    "status": LiveModelCallStatus.FAILED,
                    "latency_ms": latency_ms,
                    "error_code": _stable_error_code(error, "live-model-call-error"),
                }
            )
        self._notify()

    def call_indices(
        self,
        trial_id: str,
        *owners: LiveCallOwner,
    ) -> tuple[int, ...]:
        owner_set = set(owners)
        return tuple(
            item.call_index
            for item in self.records
            if item.trial_id == trial_id and item.owner in owner_set
        )

    def remaining_repair_calls(self, trial_id: str) -> int:
        used = sum(
            item.trial_id == trial_id and item.phase == LiveCallPhase.REPAIR
            for item in self.records
        )
        return self._budget.repair_model_call_allowance_per_trial - used


class _BudgetedSingleSelectionModel:
    def __init__(
        self,
        inner: SingleSelectionModel,
        guard: LiveCallBudgetGuard,
        *,
        trial_id: str,
        case_id: str,
        repetition: int,
    ) -> None:
        self._inner = inner
        self._guard = guard
        self._coordinates = (trial_id, case_id, repetition)

    def select(self, snapshot: ComparisonToolSnapshot) -> SingleSelectionModelResponse:
        trial_id, case_id, repetition = self._coordinates
        call_index = self._guard.begin(
            trial_id=trial_id,
            case_id=case_id,
            repetition=repetition,
            owner=LiveCallOwner.SINGLE_AGENT,
            phase=LiveCallPhase.BASE,
            node=LiveModelNode.SINGLE_SELECTION,
        )
        started = perf_counter()
        try:
            result = self._inner.select(snapshot)
        except BaseException as error:
            self._guard.fail(call_index, error, latency_ms=_elapsed_ms(started))
            raise
        self._guard.succeed(
            call_index,
            latency_ms=result.latency_ms,
            usage=result.usage,
        )
        return result


class _BudgetedExploreModel:
    def __init__(
        self,
        inner: ExploreProposalModel,
        guard: LiveCallBudgetGuard,
        *,
        trial_id: str,
        case_id: str,
        repetition: int,
        owner: LiveCallOwner,
        phase: LiveCallPhase,
    ) -> None:
        self._inner = inner
        self._guard = guard
        self._coordinates = (trial_id, case_id, repetition, owner, phase)

    def _begin(self, node: LiveModelNode, ceiling: int | None = None) -> int:
        trial_id, case_id, repetition, owner, phase = self._coordinates
        return self._guard.begin(
            trial_id=trial_id,
            case_id=case_id,
            repetition=repetition,
            owner=owner,
            phase=phase,
            node=node,
            max_completion_tokens=ceiling,
        )

    def propose_queries(self, context: PlannerContext) -> ExploreQueryModelResponse:
        call_index = self._begin(
            LiveModelNode.EXPLORE_QUERY,
            1200 if self._coordinates[-1] == LiveCallPhase.REPAIR else None,
        )
        started = perf_counter()
        try:
            result = self._inner.propose_queries(context)
        except BaseException as error:
            self._guard.fail(call_index, error, latency_ms=_elapsed_ms(started))
            raise
        self._guard.succeed(call_index, latency_ms=result.latency_ms, usage=result.usage)
        return result

    def select_candidates(
        self,
        context: PlannerContext,
        queries: tuple[ExploreSearchQuery, ...],
        observations: tuple[ExploreCandidateObservation, ...],
    ) -> ExploreSelectionModelResponse:
        call_index = self._begin(LiveModelNode.EXPLORE_SELECTION)
        started = perf_counter()
        try:
            result = self._inner.select_candidates(
                context,
                queries,
                observations,
            )
        except BaseException as error:
            self._guard.fail(call_index, error, latency_ms=_elapsed_ms(started))
            raise
        self._guard.succeed(call_index, latency_ms=result.latency_ms, usage=result.usage)
        return result


class _BudgetedStayModel:
    def __init__(
        self,
        inner: StayProposalModel,
        guard: LiveCallBudgetGuard,
        *,
        trial_id: str,
        case_id: str,
        repetition: int,
        owner: LiveCallOwner,
        phase: LiveCallPhase,
    ) -> None:
        self._inner = inner
        self._guard = guard
        self._coordinates = (trial_id, case_id, repetition, owner, phase)

    def _begin(self, node: LiveModelNode, ceiling: int | None = None) -> int:
        trial_id, case_id, repetition, owner, phase = self._coordinates
        return self._guard.begin(
            trial_id=trial_id,
            case_id=case_id,
            repetition=repetition,
            owner=owner,
            phase=phase,
            node=node,
            max_completion_tokens=ceiling,
        )

    def propose_queries(self, context: PlannerContext) -> StayQueryModelResponse:
        call_index = self._begin(
            LiveModelNode.STAY_QUERY,
            1200 if self._coordinates[-1] == LiveCallPhase.REPAIR else None,
        )
        started = perf_counter()
        try:
            result = self._inner.propose_queries(context)
        except BaseException as error:
            self._guard.fail(call_index, error, latency_ms=_elapsed_ms(started))
            raise
        self._guard.succeed(call_index, latency_ms=result.latency_ms, usage=result.usage)
        return result

    def select_candidates(
        self,
        context: PlannerContext,
        queries: tuple[StaySearchQuery, ...],
        observations: tuple[StayCandidateObservation, ...],
    ) -> StaySelectionModelResponse:
        call_index = self._begin(LiveModelNode.STAY_SELECTION)
        started = perf_counter()
        try:
            result = self._inner.select_candidates(
                context,
                queries,
                observations,
            )
        except BaseException as error:
            self._guard.fail(call_index, error, latency_ms=_elapsed_ms(started))
            raise
        self._guard.succeed(call_index, latency_ms=result.latency_ms, usage=result.usage)
        return result


class _BudgetedPlanModel:
    def __init__(
        self,
        inner: PlanProposalModel,
        guard: LiveCallBudgetGuard,
        *,
        trial_id: str,
        case_id: str,
        repetition: int,
        owner: LiveCallOwner,
        phase: LiveCallPhase,
        single: bool,
    ) -> None:
        self._inner = inner
        self._guard = guard
        self._coordinates = (trial_id, case_id, repetition, owner, phase)
        self._node = LiveModelNode.SINGLE_PLAN if single else LiveModelNode.PRODUCT_PLAN

    def propose(self, materials: PlanningMaterialBundle) -> PlannerModelResponse:
        trial_id, case_id, repetition, owner, phase = self._coordinates
        call_index = self._guard.begin(
            trial_id=trial_id,
            case_id=case_id,
            repetition=repetition,
            owner=owner,
            phase=phase,
            node=self._node,
            max_completion_tokens=1200 if phase == LiveCallPhase.REPAIR else None,
        )
        started = perf_counter()
        try:
            result = self._inner.propose(materials)
        except BaseException as error:
            self._guard.fail(call_index, error, latency_ms=_elapsed_ms(started))
            raise
        self._guard.succeed(call_index, latency_ms=result.latency_ms, usage=result.usage)
        return result


class DeepSeekSingleSelectionModel:
    def __init__(self, settings: Settings) -> None:
        api_key = require_secret(settings.deepseek_api_key, "DEEPSEEK_API_KEY")
        client = OpenAI(
            api_key=api_key,
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._client = wrap_openai(client, chat_name="DeepSeekComparisonSingleSelection")
        self._model = settings.deepseek_model

    def select(self, snapshot: ComparisonToolSnapshot) -> SingleSelectionModelResponse:
        payload = {
            "planner_context": snapshot.planner_context.model_dump(mode="json"),
            "poi_candidates": [item.model_dump(mode="json") for item in snapshot.poi_candidates],
            "stay_candidates": [item.model_dump(mode="json") for item in snapshot.stay_candidates],
            "weather_risks": [item.model_dump(mode="json") for item in snapshot.weather_risks],
            "budget_allocation": snapshot.budget_allocation.model_dump(mode="json"),
            "boundaries": {
                "provider_facts_only": True,
                "price_and_availability_unverified": True,
                "route_computed_after_selection": True,
            },
        }
        messages = cast(
            list[ChatCompletionMessageParam],
            [
                {"role": "system", "content": SINGLE_SELECTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
        )
        started = perf_counter()
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=[SINGLE_SELECTION_TOOL_SCHEMA],
            tool_choice={
                "type": "function",
                "function": {"name": SINGLE_SELECTION_TOOL_NAME},
            },
            temperature=0,
            max_tokens=900,
            extra_body={"thinking": {"type": "disabled"}},
        )
        latency_ms = _elapsed_ms(started)
        tool_calls = response.choices[0].message.tool_calls
        if tool_calls is None or len(tool_calls) != 1:
            raise LiveComparisonRunError("DeepSeek must return one Single selection tool call")
        tool_call = tool_calls[0]
        if tool_call.type != "function" or tool_call.function.name != SINGLE_SELECTION_TOOL_NAME:
            raise LiveComparisonRunError("DeepSeek returned an unexpected Single selection call")
        try:
            proposal = SingleSelectionProposal.model_validate_json(tool_call.function.arguments)
        except ValidationError as error:
            raise LiveComparisonRunError("DeepSeek returned invalid Single selection") from error
        usage = None
        if response.usage is not None:
            usage = ModelTokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )
        return SingleSelectionModelResponse(
            proposal=proposal,
            model=self._model,
            latency_ms=latency_ms,
            usage=usage,
        )


class DeepSeekLivePilotModelFactory:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def single_selection(self) -> SingleSelectionModel:
        return DeepSeekSingleSelectionModel(self._settings)

    def explore(self) -> ExploreProposalModel:
        return DeepSeekExploreProposalModel(self._settings)

    def stay(self) -> StayProposalModel:
        return DeepSeekStayProposalModel(self._settings)

    def plan(self) -> PlanProposalModel:
        return DeepSeekPlanProposalModel(self._settings)


class NoopTrialTracer:
    def trial(
        self,
        case: LiveComparisonPilotCase,
        repetition: int,
        trial_id: str,
        dataset_sha256: str,
    ) -> AbstractContextManager[str | None]:
        del case, repetition, trial_id, dataset_sha256
        return nullcontext(None)

    def flush(self) -> None:
        return None


class LangSmithTrialTracer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = build_langsmith_client(settings, TraceRedactor.from_settings(settings))

    def trial(
        self,
        case: LiveComparisonPilotCase,
        repetition: int,
        trial_id: str,
        dataset_sha256: str,
    ) -> AbstractContextManager[str | None]:
        client = self._client
        settings = self._settings

        @contextmanager
        def _trial_context() -> Iterator[str]:
            with (
                tracing_context(
                    enabled=True,
                    client=client,
                    project_name=settings.langsmith_project,
                ),
                trace(
                    "eztrip-live-system-comparison-trial-v1",
                    run_type="chain",
                    inputs={
                        "trial_id": trial_id,
                        "case_id": case.case_id,
                        "repetition": repetition,
                    },
                    client=client,
                    project_name=settings.langsmith_project,
                    tags=["ez-502b", "live-comparison", "development-pilot"],
                    metadata={
                        "dataset_sha256": dataset_sha256,
                        "model": settings.deepseek_model,
                        "temperature": 0,
                        "provider_mode": "frozen_fixture_catalogs",
                        "amap_calls": 0,
                        "raw_user_text_in_metadata": False,
                    },
                ) as run,
            ):
                yield str(run.trace_id)

        return _trial_context()

    def flush(self) -> None:
        self._client.flush(timeout=30.0)


def _rerank_explore(
    result: ExploreAgentResult,
    candidate_ids: tuple[str, ...],
) -> ExploreAgentResult:
    by_id = {item.candidate.candidate_id: item for item in result.recommendations}
    recommendations = tuple(
        ExploreRecommendation(
            proposal=by_id[candidate_id].proposal.model_copy(update={"rank": rank}),
            candidate=by_id[candidate_id].candidate,
            query_ids=by_id[candidate_id].query_ids,
        )
        for rank, candidate_id in enumerate(candidate_ids, start=1)
    )
    return ExploreAgentResult.model_validate(
        result.model_copy(update={"recommendations": recommendations}).model_dump(mode="python")
    )


def _rerank_stay(result: StayAgentResult, candidate_id: str) -> StayAgentResult:
    by_id = {item.candidate.candidate_id: item for item in result.recommendations}
    selected = by_id[candidate_id]
    recommendation = StayRecommendation(
        proposal=selected.proposal.model_copy(update={"rank": 1}),
        candidate=selected.candidate,
        query_ids=selected.query_ids,
    )
    return StayAgentResult.model_validate(
        result.model_copy(update={"recommendations": (recommendation,)}).model_dump(mode="python")
    )


def _replace_single_branches(
    materials: PlanningMaterialBundle,
    selection: SingleSelectionProposal,
) -> SpecialistFanoutResult:
    branches: list[SpecialistBranchResult] = []
    for branch in materials.specialist_result.branches:
        if branch.specialist == SpecialistName.EXPLORE:
            if branch.explore_result is None:
                raise LiveComparisonRunError("base fixture has no Explore result")
            branch = branch.model_copy(
                update={
                    "explore_result": _rerank_explore(
                        branch.explore_result,
                        selection.poi_candidate_ids,
                    )
                }
            )
        elif branch.specialist == SpecialistName.STAY:
            if branch.stay_result is None:
                raise LiveComparisonRunError("base fixture has no Stay result")
            branch = branch.model_copy(
                update={
                    "stay_result": _rerank_stay(
                        branch.stay_result,
                        selection.primary_stay_candidate_id,
                    )
                }
            )
        branches.append(branch)
    return SpecialistFanoutResult.model_validate(
        materials.specialist_result.model_copy(update={"branches": tuple(branches)}).model_dump(
            mode="python"
        )
    )


def _validate_single_selection(
    snapshot: ComparisonToolSnapshot,
    selection: SingleSelectionProposal,
) -> None:
    poi_ids = {item.candidate_id for item in snapshot.poi_candidates}
    stay_ids = {item.candidate_id for item in snapshot.stay_candidates}
    if not set(selection.poi_candidate_ids).issubset(poi_ids):
        raise LiveComparisonRunError("Single selection contains unknown POI ids")
    if selection.primary_stay_candidate_id not in stay_ids:
        raise LiveComparisonRunError("Single selection contains an unknown stay id")


async def _single_materials(
    base: PlanningMaterialBundle,
    selection: SingleSelectionProposal,
) -> tuple[PlanningMaterialBundle, int]:
    route_provider = PlanningMaterialRouteProvider(RouteFailureInjection.NONE)
    materials = await build_planning_material_bundle(
        _replace_single_branches(base, selection),
        route_provider,
    )
    return materials, len(route_provider.calls)


class _PilotRepairPipeline(ProductRepairPipeline):
    def __init__(
        self,
        provider: SpecialistScenarioProvider,
        explore_model: ExploreProposalModel,
        stay_model: StayProposalModel,
        plan_model: PlanProposalModel,
    ) -> None:
        self._provider = provider
        self._explore_model = explore_model
        self._stay_model = stay_model
        self._plan_model = plan_model
        self.route_provider_call_count = 0

    async def rerun_explore(self, context: object) -> ExploreAgentResult:
        return await run_explore_agent(context, self._provider, self._explore_model)  # type: ignore[arg-type]

    async def rerun_stay(self, context: object) -> StayAgentResult:
        return await run_stay_agent(context, self._provider, self._stay_model)  # type: ignore[arg-type]

    async def build_materials(
        self,
        specialist_result: SpecialistFanoutResult,
    ) -> PlanningMaterialBundle:
        route_provider = PlanningMaterialRouteProvider(RouteFailureInjection.NONE)
        result = await build_planning_material_bundle(specialist_result, route_provider)
        self.route_provider_call_count += len(route_provider.calls)
        return result

    def run_plan(
        self,
        request: TripRequest,
        materials: PlanningMaterialBundle,
    ) -> PlanAgentRunResult:
        return run_plan_agent(request, materials, self._plan_model)

    def build_opening_hours(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        data_mode: DataMode,
    ) -> OpeningHoursEvidenceBundle:
        if data_mode != DataMode.FIXTURE:
            raise LiveComparisonRunError("live pilot repair must preserve fixture data mode")
        return build_clean_comparison_opening_hours(request, plan)


class _BudgetedRepairExecutor:
    _PREDICTED_CALLS: ClassVar[dict[RepairAction, int]] = {
        RepairAction.RERUN_EXPLORE: 3,
        RepairAction.RERUN_STAY: 3,
        RepairAction.RERUN_ROUTE: 1,
        RepairAction.RECALCULATE_BUDGET: 1,
        RepairAction.REPLAN_DAY: 1,
    }

    def __init__(
        self,
        inner: ProductRepairExecutor,
        guard: LiveCallBudgetGuard,
        trial_id: str,
    ) -> None:
        self._inner = inner
        self._guard = guard
        self._trial_id = trial_id

    async def repair(
        self,
        request: TripRequest,
        plan: TripPlan,
        materials: PlanningMaterialBundle,
        opening_hours: OpeningHoursEvidenceBundle,
        issues: tuple[ValidationIssue, ...],
        repair_action: RepairAction,
        action_attempt: int,
    ) -> RepairExecutionResult:
        predicted = self._PREDICTED_CALLS.get(repair_action, 3)
        if predicted > self._guard.remaining_repair_calls(self._trial_id):
            return RepairExecutionResult(
                status=RepairExecutionStatus.FAILED,
                materials=materials,
                plan=plan,
                opening_hours=opening_hours,
                error_code="live-repair-call-budget-exceeded",
            )
        return await self._inner.repair(
            request,
            plan,
            materials,
            opening_hours,
            issues,
            repair_action,
            action_attempt,
        )


def _error_codes(report: PlanValidationReport) -> tuple[str, ...]:
    return tuple(item.rule_code for item in report.issues if item.severity == IssueSeverity.ERROR)


def _plan_sha256(plan: TripPlan) -> str:
    return _sha256(plan.model_dump(mode="json"))


def _catalog_sha256(snapshot: ComparisonToolSnapshot) -> str:
    return _sha256(
        {
            "planner_context": snapshot.planner_context.model_dump(mode="json"),
            "poi_candidates": [item.model_dump(mode="json") for item in snapshot.poi_candidates],
            "stay_candidates": [item.model_dump(mode="json") for item in snapshot.stay_candidates],
            "weather_risks": [item.model_dump(mode="json") for item in snapshot.weather_risks],
            "budget_allocation": snapshot.budget_allocation.model_dump(mode="json"),
        }
    )


def _candidate_evidence(
    plan: TripPlan,
    materials: PlanningMaterialBundle,
) -> tuple[int, int, int, int]:
    candidates = {item.candidate_id: item for item in materials.shortlist.poi_candidates}
    edges = {item.edge_id: item for item in materials.route_matrix.edges}
    scheduled = tuple(
        item for day in plan.days for item in day.items if item.candidate_id is not None
    )
    grounded = tuple(
        item
        for item in scheduled
        if item.candidate_id in candidates
        and item.title == candidates[item.candidate_id].name
        and item.source == candidates[item.candidate_id].source
    )
    traceable = tuple(
        item
        for item in grounded
        if item.source is not None
        and item.source.provider_id is not None
        and item.source.data_mode == DataMode.FIXTURE
    )
    route_backed = tuple(
        item
        for item in traceable
        if item.route_from_previous is not None
        and any(
            edge.status == RouteEdgeStatus.SUCCEEDED and edge.route == item.route_from_previous
            for edge in edges.values()
        )
    )
    return len(scheduled), len(grounded), len(traceable), len(route_backed)


def _call_totals(
    records: tuple[LiveModelCallRecord, ...],
    indices: tuple[int, ...],
) -> tuple[bool, int | None, int | None, int | None, int]:
    by_index = {item.call_index: item for item in records}
    selected = tuple(by_index[index] for index in indices)
    complete = all(item.usage is not None for item in selected)
    prompt = sum(item.usage.prompt_tokens for item in selected if item.usage is not None)
    completion = sum(item.usage.completion_tokens for item in selected if item.usage is not None)
    total = sum(item.usage.total_tokens for item in selected if item.usage is not None)
    latency = sum(item.latency_ms or 0 for item in selected)
    return (
        complete,
        prompt if complete else None,
        completion if complete else None,
        total if complete else None,
        latency,
    )


def _label_counts(
    selected_poi_ids: tuple[str, ...],
    selected_stay_id: str,
    source: PlanAgentEvalCase,
) -> tuple[int, int, int, int, bool]:
    explore = next(
        item
        for item in load_explore_agent_suite().cases
        if item.case_id == source.explore_fixture_case_id
    )
    stay = next(
        item
        for item in load_stay_agent_suite().cases
        if item.case_id == source.stay_fixture_case_id
    )
    selected = set(selected_poi_ids)
    allowed = len(selected & set(explore.expected.allowed_recommendation_ids))
    matched_groups = sum(
        bool(selected & set(group)) for group in explore.expected.required_recommendation_groups
    )
    return (
        len(selected_poi_ids),
        allowed,
        len(explore.expected.required_recommendation_groups),
        matched_groups,
        selected_stay_id in set(stay.expected.allowed_recommendation_ids),
    )


def _successful_arm(
    *,
    arm: ComparisonArm,
    source: PlanAgentEvalCase,
    materials: PlanningMaterialBundle,
    initial_plan: TripPlan,
    final_plan: TripPlan,
    initial_report: PlanValidationReport,
    final_report: PlanValidationReport,
    outcome: LiveTrialOutcome,
    call_indices: tuple[int, ...],
    records: tuple[LiveModelCallRecord, ...],
    repair: RepairRouterResult | None = None,
) -> LiveArmTrialResult:
    selected_poi_ids = tuple(item.candidate_id for item in materials.shortlist.poi_candidates)
    stay = materials.shortlist.primary_stay
    if stay is None:
        raise LiveComparisonRunError("successful arm requires a primary stay")
    labels = _label_counts(selected_poi_ids, stay.candidate_id, source)
    evidence = _candidate_evidence(final_plan, materials)
    usage = _call_totals(records, call_indices)
    return LiveArmTrialResult(
        arm=arm,
        execution_succeeded=True,
        outcome=outcome,
        selected_poi_candidate_ids=selected_poi_ids,
        selected_stay_candidate_id=stay.candidate_id,
        initial_plan_sha256=_plan_sha256(initial_plan),
        final_plan_sha256=_plan_sha256(final_plan),
        initial_error_codes=_error_codes(initial_report),
        final_error_codes=_error_codes(final_report),
        final_can_finalize=final_report.can_finalize,
        repair_actions=(
            tuple(item.repair_action for item in repair.attempts) if repair is not None else ()
        ),
        repair_stop_reason=repair.stop_reason if repair is not None else None,
        selected_poi_count=labels[0],
        allowed_poi_count=labels[1],
        required_poi_group_count=labels[2],
        matched_poi_group_count=labels[3],
        stay_selection_allowed=labels[4],
        scheduled_candidate_count=evidence[0],
        grounded_candidate_count=evidence[1],
        traceable_candidate_count=evidence[2],
        route_backed_candidate_count=evidence[3],
        logical_model_call_indices=call_indices,
        logical_model_call_count=len(call_indices),
        token_usage_complete=usage[0],
        prompt_tokens=usage[1],
        completion_tokens=usage[2],
        total_tokens=usage[3],
        model_latency_ms=usage[4],
    )


def _failed_arm(
    arm: ComparisonArm,
    error: BaseException,
    call_indices: tuple[int, ...],
    records: tuple[LiveModelCallRecord, ...],
) -> LiveArmTrialResult:
    usage = _call_totals(records, call_indices)
    return LiveArmTrialResult(
        arm=arm,
        execution_succeeded=False,
        outcome=LiveTrialOutcome.EXECUTION_FAILED,
        error_code=_stable_error_code(error),
        selected_poi_count=0,
        allowed_poi_count=0,
        required_poi_group_count=0,
        matched_poi_group_count=0,
        scheduled_candidate_count=0,
        grounded_candidate_count=0,
        traceable_candidate_count=0,
        route_backed_candidate_count=0,
        logical_model_call_indices=call_indices,
        logical_model_call_count=len(call_indices),
        token_usage_complete=usage[0],
        prompt_tokens=usage[1],
        completion_tokens=usage[2],
        total_tokens=usage[3],
        model_latency_ms=usage[4],
    )


def _no_gate_outcome(report: PlanValidationReport) -> LiveTrialOutcome:
    return (
        LiveTrialOutcome.FINALIZABLE_WITHOUT_REPAIR
        if report.can_finalize
        else LiveTrialOutcome.UNRESOLVED
    )


def _repair_outcome(result: RepairRouterResult) -> LiveTrialOutcome:
    return {
        RepairOutcome.ALREADY_FINALIZABLE: LiveTrialOutcome.FINALIZABLE_WITHOUT_REPAIR,
        RepairOutcome.REPAIRED: LiveTrialOutcome.REPAIRED,
        RepairOutcome.WAITING_FOR_USER: LiveTrialOutcome.WAITING_FOR_USER,
        RepairOutcome.UNRESOLVED: LiveTrialOutcome.UNRESOLVED,
    }[result.outcome]


def _fixture_reads(provider: SpecialistScenarioProvider, route_calls: int) -> int:
    return provider.poi_calls + provider.stay_calls + provider.weather_calls + route_calls


async def _run_trial(
    *,
    case: LiveComparisonPilotCase,
    source: PlanAgentEvalCase,
    repetition: int,
    base_materials: PlanningMaterialBundle,
    snapshot: ComparisonToolSnapshot,
    factory: LivePilotModelFactory,
    guard: LiveCallBudgetGuard,
    tracer: TrialTracer,
    dataset_sha256: str,
) -> LiveComparisonTrialResult:
    trial_id = f"{case.case_id}-r{repetition}"
    trial_started = perf_counter()
    first_call_index = len(guard.records) + 1
    single_route_calls = 0
    product_route_calls = 0
    provider = SpecialistScenarioProvider(
        snapshot.poi_candidates,
        snapshot.stay_candidates,
        snapshot.weather_risks,
        failure=None,
        require_parallel_entry=False,
    )
    with tracer.trial(case, repetition, trial_id, dataset_sha256) as trace_id:
        try:
            selection_model = _BudgetedSingleSelectionModel(
                factory.single_selection(),
                guard,
                trial_id=trial_id,
                case_id=case.case_id,
                repetition=repetition,
            )
            selection = selection_model.select(snapshot).proposal
            _validate_single_selection(snapshot, selection)
            single_materials, single_route_calls = await _single_materials(
                base_materials,
                selection,
            )
            single_plan_result = run_plan_agent(
                source.request,
                single_materials,
                _BudgetedPlanModel(
                    factory.plan(),
                    guard,
                    trial_id=trial_id,
                    case_id=case.case_id,
                    repetition=repetition,
                    owner=LiveCallOwner.SINGLE_AGENT,
                    phase=LiveCallPhase.BASE,
                    single=True,
                ),
            )
            if (
                single_plan_result.status != PlanAgentRunStatus.PLANNED
                or single_plan_result.plan is None
            ):
                raise LiveComparisonRunError("Single Agent did not produce a complete TripPlan")
            single_opening = build_clean_comparison_opening_hours(
                source.request,
                single_plan_result.plan,
            )
            single_validation = validate_hard_trip_plan(
                source.request,
                single_plan_result.plan,
                single_materials,
                single_opening,
            )
            single_indices = guard.call_indices(trial_id, LiveCallOwner.SINGLE_AGENT)
            single_arm = _successful_arm(
                arm=ComparisonArm.SINGLE_AGENT_TOOLS,
                source=source,
                materials=single_materials,
                initial_plan=single_plan_result.plan,
                final_plan=single_plan_result.plan,
                initial_report=single_validation,
                final_report=single_validation,
                outcome=_no_gate_outcome(single_validation),
                call_indices=single_indices,
                records=guard.records,
            )
        except BaseException as error:
            single_indices = guard.call_indices(trial_id, LiveCallOwner.SINGLE_AGENT)
            single_arm = _failed_arm(
                ComparisonArm.SINGLE_AGENT_TOOLS,
                error,
                single_indices,
                guard.records,
            )

        product_initial_sha: str | None = None
        try:
            explore_inner = factory.explore()
            stay_inner = factory.stay()
            plan_inner = factory.plan()
            product_explore = _BudgetedExploreModel(
                explore_inner,
                guard,
                trial_id=trial_id,
                case_id=case.case_id,
                repetition=repetition,
                owner=LiveCallOwner.PRODUCT_SHARED_INITIAL,
                phase=LiveCallPhase.BASE,
            )
            product_stay = _BudgetedStayModel(
                stay_inner,
                guard,
                trial_id=trial_id,
                case_id=case.case_id,
                repetition=repetition,
                owner=LiveCallOwner.PRODUCT_SHARED_INITIAL,
                phase=LiveCallPhase.BASE,
            )
            specialist_result = await run_specialist_fanout(
                source.request,
                provider,
                product_explore,
                product_stay,
                data_mode=DataMode.FIXTURE,
            )
            product_route_provider = PlanningMaterialRouteProvider(RouteFailureInjection.NONE)
            product_materials = await build_planning_material_bundle(
                specialist_result,
                product_route_provider,
            )
            product_route_calls += len(product_route_provider.calls)
            product_plan_result = run_plan_agent(
                source.request,
                product_materials,
                _BudgetedPlanModel(
                    plan_inner,
                    guard,
                    trial_id=trial_id,
                    case_id=case.case_id,
                    repetition=repetition,
                    owner=LiveCallOwner.PRODUCT_SHARED_INITIAL,
                    phase=LiveCallPhase.BASE,
                    single=False,
                ),
            )
            if (
                product_plan_result.status != PlanAgentRunStatus.PLANNED
                or product_plan_result.plan is None
            ):
                raise LiveComparisonRunError("Product initial stage did not produce a TripPlan")
            product_plan = product_plan_result.plan
            product_initial_sha = _plan_sha256(product_plan)
            product_opening = build_clean_comparison_opening_hours(
                source.request,
                product_plan,
            )
            product_validation = validate_hard_trip_plan(
                source.request,
                product_plan,
                product_materials,
                product_opening,
            )
            shared_indices = guard.call_indices(
                trial_id,
                LiveCallOwner.PRODUCT_SHARED_INITIAL,
            )
            no_gate_arm = _successful_arm(
                arm=ComparisonArm.PRODUCT_GRAPH_NO_HARD_GATE,
                source=source,
                materials=product_materials,
                initial_plan=product_plan,
                final_plan=product_plan,
                initial_report=product_validation,
                final_report=product_validation,
                outcome=_no_gate_outcome(product_validation),
                call_indices=shared_indices,
                records=guard.records,
            )

            repair_explore = _BudgetedExploreModel(
                explore_inner,
                guard,
                trial_id=trial_id,
                case_id=case.case_id,
                repetition=repetition,
                owner=LiveCallOwner.PRODUCT_REPAIR,
                phase=LiveCallPhase.REPAIR,
            )
            repair_stay = _BudgetedStayModel(
                stay_inner,
                guard,
                trial_id=trial_id,
                case_id=case.case_id,
                repetition=repetition,
                owner=LiveCallOwner.PRODUCT_REPAIR,
                phase=LiveCallPhase.REPAIR,
            )
            repair_plan = _BudgetedPlanModel(
                plan_inner,
                guard,
                trial_id=trial_id,
                case_id=case.case_id,
                repetition=repetition,
                owner=LiveCallOwner.PRODUCT_REPAIR,
                phase=LiveCallPhase.REPAIR,
                single=False,
            )
            pipeline = _PilotRepairPipeline(
                provider,
                repair_explore,
                repair_stay,
                repair_plan,
            )
            repair = await run_repair_router(
                source.request,
                product_plan,
                product_materials,
                product_opening,
                _BudgetedRepairExecutor(
                    ProductRepairExecutor(pipeline),
                    guard,
                    trial_id,
                ),
            )
            product_route_calls += pipeline.route_provider_call_count
            full_indices = guard.call_indices(
                trial_id,
                LiveCallOwner.PRODUCT_SHARED_INITIAL,
                LiveCallOwner.PRODUCT_REPAIR,
            )
            full_arm = _successful_arm(
                arm=ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR,
                source=source,
                materials=repair.final_materials,
                initial_plan=product_plan,
                final_plan=repair.final_plan,
                initial_report=product_validation,
                final_report=repair.final_report,
                outcome=_repair_outcome(repair),
                call_indices=full_indices,
                records=guard.records,
                repair=repair,
            )
        except BaseException as error:
            shared_indices = guard.call_indices(
                trial_id,
                LiveCallOwner.PRODUCT_SHARED_INITIAL,
            )
            full_indices = guard.call_indices(
                trial_id,
                LiveCallOwner.PRODUCT_SHARED_INITIAL,
                LiveCallOwner.PRODUCT_REPAIR,
            )
            no_gate_arm = _failed_arm(
                ComparisonArm.PRODUCT_GRAPH_NO_HARD_GATE,
                error,
                shared_indices,
                guard.records,
            )
            full_arm = _failed_arm(
                ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR,
                error,
                full_indices,
                guard.records,
            )
            product_initial_sha = None
    tracer.flush()
    last_call_index = len(guard.records)
    return LiveComparisonTrialResult(
        trial_id=trial_id,
        case_id=case.case_id,
        repetition=repetition,
        trace_id=trace_id,
        input_catalog_sha256=_catalog_sha256(snapshot),
        product_initial_plan_sha256=product_initial_sha,
        product_initial_draft_shared=True,
        external_provider_call_count=0,
        fixture_provider_read_count=(
            single_route_calls + _fixture_reads(provider, product_route_calls)
        ),
        physical_model_call_indices=tuple(range(first_call_index, last_call_index + 1)),
        wall_clock_ms=_elapsed_ms(trial_started),
        arms=(single_arm, no_gate_arm, full_arm),
    )


def _nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    rank = (percentile * len(values) + 99) // 100
    return sorted(values)[max(rank - 1, 0)]


def _arm_summary(
    arm: ComparisonArm,
    trials: tuple[LiveComparisonTrialResult, ...],
) -> LiveComparisonArmSummary:
    results = tuple(next(item for item in trial.arms if item.arm == arm) for trial in trials)
    succeeded = tuple(item for item in results if item.execution_succeeded)
    finalizable = sum(item.final_can_finalize is True for item in results)
    selected = sum(item.selected_poi_count for item in succeeded)
    allowed = sum(item.allowed_poi_count for item in succeeded)
    required_groups = sum(item.required_poi_group_count for item in succeeded)
    matched_groups = sum(item.matched_poi_group_count for item in succeeded)
    scheduled = sum(item.scheduled_candidate_count for item in succeeded)
    grouped: dict[str, list[LiveArmTrialResult]] = {}
    for trial, result in zip(trials, results, strict=True):
        grouped.setdefault(trial.case_id, []).append(result)
    comparable = tuple(
        pair
        for pair in grouped.values()
        if len(pair) == 2 and all(item.execution_succeeded for item in pair)
    )
    consistent = sum(
        pair[0].final_plan_sha256 == pair[1].final_plan_sha256
        and pair[0].selected_poi_candidate_ids == pair[1].selected_poi_candidate_ids
        and pair[0].selected_stay_candidate_id == pair[1].selected_stay_candidate_id
        for pair in comparable
    )
    latencies = [item.model_latency_ms for item in results]
    return LiveComparisonArmSummary(
        arm=arm,
        execution_succeeded_trial_count=len(succeeded),
        finalizable_trial_count=finalizable,
        finalization_rate=summarize_rate(finalizable, len(succeeded)),
        selected_poi_count=selected,
        allowed_poi_count=allowed,
        labelled_relevance_rate=summarize_rate(allowed, selected),
        required_poi_group_count=required_groups,
        matched_poi_group_count=matched_groups,
        group_coverage_rate=summarize_rate(matched_groups, required_groups),
        allowed_stay_trial_count=sum(item.stay_selection_allowed is True for item in succeeded),
        grounding_rate=summarize_rate(
            sum(item.grounded_candidate_count for item in succeeded),
            scheduled,
        ),
        route_lineage_rate=summarize_rate(
            sum(item.route_backed_candidate_count for item in succeeded),
            scheduled,
        ),
        logical_model_call_count=sum(item.logical_model_call_count for item in results),
        exact_plan_consistency_case_count=consistent,
        comparable_consistency_case_count=len(comparable),
        p50_cumulative_model_latency_ms=_nearest_rank(latencies, 50),
        p95_cumulative_model_latency_ms=_nearest_rank(latencies, 95),
    )


def _paired_delta(
    left: ComparisonArm,
    right: ComparisonArm,
    trials: tuple[LiveComparisonTrialResult, ...],
) -> LiveComparisonPairedDelta:
    pairs = tuple(
        (
            next(item for item in trial.arms if item.arm == left),
            next(item for item in trial.arms if item.arm == right),
        )
        for trial in trials
    )
    evaluable = tuple(
        pair for pair in pairs if pair[0].execution_succeeded and pair[1].execution_succeeded
    )
    improved = sum(
        pair[0].final_can_finalize is False and pair[1].final_can_finalize is True
        for pair in evaluable
    )
    worsened = sum(
        pair[0].final_can_finalize is True and pair[1].final_can_finalize is False
        for pair in evaluable
    )
    left_rate = summarize_rate(
        sum(pair[0].final_can_finalize is True for pair in evaluable),
        len(evaluable),
    )
    right_rate = summarize_rate(
        sum(pair[1].final_can_finalize is True for pair in evaluable),
        len(evaluable),
    )
    return LiveComparisonPairedDelta(
        from_arm=left,
        to_arm=right,
        shared_evaluable_trial_count=len(evaluable),
        improved_trial_count=improved,
        worsened_trial_count=worsened,
        unchanged_trial_count=len(evaluable) - improved - worsened,
        finalization_rate_delta=(right_rate - left_rate).quantize(Decimal("0.0001")),
    )


class LiveJournalWriter:
    def __init__(
        self,
        path: Path,
        *,
        dataset_sha256: str,
        model: str,
        started_at: datetime,
    ) -> None:
        self._path = path
        self._dataset_sha256 = dataset_sha256
        self._model = model
        self._started_at = started_at
        self._current_trial_id: str | None = None
        self._calls: tuple[LiveModelCallRecord, ...] = ()
        self._trials: tuple[LiveComparisonTrialResult, ...] = ()
        self._lock = Lock()

    def set_current_trial(self, trial_id: str) -> None:
        with self._lock:
            self._current_trial_id = trial_id
            self._write(LiveRunJournalStatus.RUNNING)

    def update_calls(self, calls: tuple[LiveModelCallRecord, ...]) -> None:
        with self._lock:
            merged = {item.call_index: item for item in self._calls}
            for item in calls:
                previous = merged.get(item.call_index)
                if (
                    previous is not None
                    and previous.status != LiveModelCallStatus.STARTED
                    and item.status == LiveModelCallStatus.STARTED
                ):
                    continue
                merged[item.call_index] = item
            self._calls = tuple(merged[index] for index in sorted(merged))
            self._write(LiveRunJournalStatus.RUNNING)

    def add_trial(self, trial: LiveComparisonTrialResult) -> None:
        with self._lock:
            self._trials = (*self._trials, trial)
            self._current_trial_id = None
            self._write(LiveRunJournalStatus.RUNNING)

    def fail(self, error: BaseException) -> None:
        with self._lock:
            self._write(
                LiveRunJournalStatus.FAILED,
                failure_code=_stable_error_code(error, "live-run-failure"),
            )

    def complete(self) -> None:
        with self._lock:
            self._write(LiveRunJournalStatus.COMPLETED)

    def _write(
        self,
        status: LiveRunJournalStatus,
        *,
        failure_code: str | None = None,
    ) -> None:
        journal = LiveComparisonRunJournal(
            status=status,
            dataset_sha256=self._dataset_sha256,
            model=self._model,
            started_at=self._started_at,
            updated_at=datetime.now(UTC),
            current_trial_id=self._current_trial_id,
            completed_trial_count=len(self._trials),
            calls=self._calls,
            trials=self._trials,
            failure_code=failure_code,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(
            json.dumps(journal.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(self._path)


async def evaluate_live_comparison_pilot(
    factory: LivePilotModelFactory,
    *,
    model: str,
    tracer: TrialTracer | None = None,
    journal_path: Path | None = None,
    execution_mode: LiveExecutionMode = LiveExecutionMode.FIXTURE_CONTRACT,
) -> LiveComparisonPilotReport:
    suite = load_live_comparison_pilot_suite()
    if model != suite.model_name:
        raise LiveComparisonRunError("live runner model must match the frozen suite")
    dataset_sha256 = live_comparison_pilot_dataset_sha256(suite)
    started_at = datetime.now(UTC)
    journal = (
        LiveJournalWriter(
            journal_path,
            dataset_sha256=dataset_sha256,
            model=model,
            started_at=started_at,
        )
        if journal_path is not None
        else None
    )
    guard = LiveCallBudgetGuard(
        suite.call_budget,
        model,
        on_change=journal.update_calls if journal is not None else None,
    )
    active_tracer = tracer or NoopTrialTracer()
    plan_by_id = {item.case_id: item for item in load_plan_agent_suite().cases}
    trials: list[LiveComparisonTrialResult] = []
    try:
        for case in suite.cases:
            source = plan_by_id[case.source_plan_case_id]
            base_materials = await build_plan_agent_materials(source)
            snapshot = build_comparison_tool_snapshot(base_materials)
            for repetition in range(1, suite.repetitions_per_case + 1):
                trial_id = f"{case.case_id}-r{repetition}"
                if journal is not None:
                    journal.set_current_trial(trial_id)
                trial = await _run_trial(
                    case=case,
                    source=source,
                    repetition=repetition,
                    base_materials=base_materials,
                    snapshot=snapshot,
                    factory=factory,
                    guard=guard,
                    tracer=active_tracer,
                    dataset_sha256=dataset_sha256,
                )
                trials.append(trial)
                if journal is not None:
                    journal.add_trial(trial)
    except BaseException as error:
        if journal is not None:
            journal.fail(error)
        active_tracer.flush()
        raise
    active_tracer.flush()
    frozen_trials = tuple(trials)
    records = guard.records
    complete_usage = all(item.usage is not None for item in records)
    summaries = tuple(_arm_summary(arm, frozen_trials) for arm in COMPARISON_ARMS)
    pairs = (
        (ComparisonArm.SINGLE_AGENT_TOOLS, ComparisonArm.PRODUCT_GRAPH_NO_HARD_GATE),
        (
            ComparisonArm.PRODUCT_GRAPH_NO_HARD_GATE,
            ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR,
        ),
        (ComparisonArm.SINGLE_AGENT_TOOLS, ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR),
    )
    report = LiveComparisonPilotReport(
        dataset_sha256=dataset_sha256,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        execution_mode=execution_mode,
        live_calls_performed=execution_mode == LiveExecutionMode.LIVE,
        langsmith_tracing_enabled=execution_mode == LiveExecutionMode.LIVE,
        physical_model_call_count=len(records),
        succeeded_model_call_count=sum(
            item.status == LiveModelCallStatus.SUCCEEDED for item in records
        ),
        failed_model_call_count=sum(item.status == LiveModelCallStatus.FAILED for item in records),
        completion_token_reservation=sum(item.max_completion_tokens for item in records),
        token_usage_complete=complete_usage,
        actual_prompt_tokens=(
            sum(item.usage.prompt_tokens for item in records if item.usage is not None)
            if complete_usage
            else None
        ),
        actual_completion_tokens=(
            sum(item.usage.completion_tokens for item in records if item.usage is not None)
            if complete_usage
            else None
        ),
        actual_total_tokens=(
            sum(item.usage.total_tokens for item in records if item.usage is not None)
            if complete_usage
            else None
        ),
        calls=records,
        trials=frozen_trials,
        arms=summaries,
        paired_deltas=tuple(_paired_delta(left, right, frozen_trials) for left, right in pairs),
        limitations=(
            "三个案例来自既有提示词开发集, 结果仅是 point-in-time repeated development pilot。",
            "模型运行使用冻结 Provider catalogs 和本地路线 fixture; "
            "高德及其他外部旅行 Provider 调用为零。",
            "两个 Product arms 共享同一初始草案; arm logical calls 会重复引用"
            "共享物理调用, 实际成本只看 physical totals。",
            "temperature=0 不保证服务端完全确定; 两次重复只用于展示本小样本的稳定性。",
            "本报告不支持泛化、真实用户成功率、实时价格/库存或生产 SLA 结论。",
        ),
    )
    if journal is not None:
        journal.complete()
    return report


async def run_live_comparison_pilot(
    settings: Settings,
    *,
    journal_path: Path | None = None,
) -> LiveComparisonPilotReport:
    if not settings.langsmith_tracing:
        raise LiveComparisonRunError("LANGSMITH_TRACING must be true for the live pilot")
    require_secret(settings.deepseek_api_key, "DEEPSEEK_API_KEY")
    require_secret(settings.langsmith_api_key, "LANGSMITH_API_KEY")
    return await evaluate_live_comparison_pilot(
        DeepSeekLivePilotModelFactory(settings),
        model=settings.deepseek_model,
        tracer=LangSmithTrialTracer(settings),
        journal_path=journal_path,
        execution_mode=LiveExecutionMode.LIVE,
    )


class FixtureSingleSelectionModel:
    def select(self, snapshot: ComparisonToolSnapshot) -> SingleSelectionModelResponse:
        return SingleSelectionModelResponse(
            proposal=SingleSelectionProposal(
                poi_candidate_ids=tuple(item.candidate_id for item in snapshot.poi_candidates),
                primary_stay_candidate_id=snapshot.stay_candidates[0].candidate_id,
                reason="fixture Single selection covers the frozen catalog.",
            ),
            model="fixture-live-pilot-model",
            latency_ms=10,
            usage=ModelTokenUsage(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            ),
        )


class FixtureLivePilotModelFactory:
    def single_selection(self) -> SingleSelectionModel:
        return FixtureSingleSelectionModel()

    def explore(self) -> ExploreProposalModel:
        from app.evaluation.planning_materials import PlanningMaterialFixtureExploreModel

        return PlanningMaterialFixtureExploreModel()

    def stay(self) -> StayProposalModel:
        from app.evaluation.planning_materials import PlanningMaterialFixtureStayModel

        return PlanningMaterialFixtureStayModel()

    def plan(self) -> PlanProposalModel:
        return PlanAgentFixtureModel()


__all__ = [
    "SINGLE_SELECTION_PROMPT_VERSION",
    "DeepSeekLivePilotModelFactory",
    "DeepSeekSingleSelectionModel",
    "FixtureLivePilotModelFactory",
    "FixtureSingleSelectionModel",
    "LangSmithTrialTracer",
    "LiveCallBudgetExceeded",
    "LiveCallBudgetGuard",
    "LiveComparisonRunError",
    "LiveJournalWriter",
    "LivePilotModelFactory",
    "NoopTrialTracer",
    "SingleSelectionModel",
    "evaluate_live_comparison_pilot",
    "run_live_comparison_pilot",
]
