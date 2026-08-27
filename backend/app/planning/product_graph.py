import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt
from pydantic import ValidationError

from app.agents.plan_agent_contracts import PlanAgentRunResult, PlanAgentRunStatus
from app.domain.money import CostItem
from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.travel_data import RouteLeg
from app.domain.validation import PlanValidationReport
from app.planning.hard_validator import validate_hard_trip_plan
from app.planning.material_contracts import PlanningMaterialBundle, PlanningMaterialIssueCode
from app.planning.plan_revision import apply_activity_replacement, apply_plan_revision
from app.planning.product_contracts import (
    ProductPlanningData,
    ProductPlanningEvent,
    ProductPlanningNodeName,
    ProductPlanningProgress,
    ProductPlanningSnapshot,
)
from app.planning.product_repair import ProductRepairExecutor, ProductRepairPipeline
from app.planning.repair_router import run_repair_router
from app.planning.review_copy import build_review_prompt
from app.planning.specialist_contracts import SpecialistFanoutResult
from app.planning.stateful_contracts import (
    Clock,
    HumanReviewAction,
    HumanReviewDecision,
    HumanReviewKind,
    HumanReviewRequest,
    HumanReviewResume,
    PlanningThreadStatus,
    StatefulPlanningNodeOutcome,
    utc_now,
)
from app.planning.weather_indoor_recovery_contracts import WeatherIndoorRecoveryResult
from app.providers.ports import RouteRequest

PRODUCT_PLANNING_GRAPH_NAME = "eztrip-product-planning-graph-v2"
LIVE_REVIEW_ONLY_RULE_CODES = frozenset(
    {
        "opening_hours.evidence_missing",
        "route.excessive_transfer",
        "route.missing_for_grounded_item",
    }
)
ProductProgressCallback = Callable[[ProductPlanningProgress], Awaitable[None]]


def should_skip_live_repair(
    data_mode: DataMode,
    validation: PlanValidationReport,
) -> bool:
    """Preserve a usable live draft when another paid pass is unlikely to resolve it.

    These issues already carry enough evidence for human review. Replaying Explore,
    route construction, and Plan can exhaust the task deadline while discarding an
    otherwise useful draft. Fixture mode still exercises deterministic repair.
    """

    if data_mode != DataMode.LIVE or validation.can_finalize:
        return False
    error_codes = frozenset(
        issue.rule_code for issue in validation.issues if issue.severity.value == "error"
    )
    return bool(error_codes) and error_codes <= LIVE_REVIEW_ONLY_RULE_CODES


class ProductPlanningProtocolError(RuntimeError):
    """Raised when the product graph or persisted checkpoint violates its contract."""


class ProductPlanningMaterialsBlockedError(ProductPlanningProtocolError):
    """Raised when grounded Provider materials cannot support a complete TripPlan."""

    def __init__(self, issues: tuple[PlanningMaterialIssueCode, ...]) -> None:
        self.issues = issues
        issue_codes = ",".join(item.value for item in issues)
        super().__init__(f"product planning materials are blocked: {issue_codes}")


class DuplicateProductPlanningThreadError(ProductPlanningProtocolError):
    """Raised when a caller tries to replace an existing product checkpoint."""


class ProductPlanningPipeline(ProductRepairPipeline, Protocol):
    async def run_specialists(
        self,
        request: TripRequest,
        *,
        data_mode: DataMode,
    ) -> SpecialistFanoutResult: ...

    async def build_materials(
        self,
        specialist_result: SpecialistFanoutResult,
    ) -> PlanningMaterialBundle: ...

    def run_plan(
        self,
        request: TripRequest,
        materials: PlanningMaterialBundle,
    ) -> PlanAgentRunResult: ...

    def build_opening_hours(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        data_mode: DataMode,
    ) -> OpeningHoursEvidenceBundle: ...

    async def get_revision_route(
        self,
        request: TripRequest,
        route_request: RouteRequest,
        data_mode: DataMode,
    ) -> RouteLeg: ...

    async def recover_weather_indoor_candidates(
        self,
        request: TripRequest,
        plan: TripPlan,
        materials: PlanningMaterialBundle,
        data_mode: DataMode,
    ) -> WeatherIndoorRecoveryResult: ...


class ProductPlanningGraphState(TypedDict):
    state: dict[str, object]


def _require_state(graph_state: ProductPlanningGraphState) -> ProductPlanningData:
    raw_state = graph_state.get("state")
    if raw_state is None:
        raise ProductPlanningProtocolError("product planning node received no state")
    try:
        return ProductPlanningData.model_validate(raw_state)
    except ValidationError as error:
        raise ProductPlanningProtocolError("product planning checkpoint is invalid") from error


def _state_update(state: ProductPlanningData) -> dict[str, object]:
    return state.model_dump(mode="json")


def _evolve_state(current: ProductPlanningData, **updates: object) -> ProductPlanningData:
    payload = current.model_dump(mode="python")
    payload.update(updates)
    return ProductPlanningData.model_validate(payload)


def _review_id(state: ProductPlanningData) -> str:
    validation = (
        state.revision_result.validation
        if state.revision_result is not None
        else state.validation
    )
    if validation is None:
        raise ProductPlanningProtocolError("product review requires hard validation")
    material = (
        f"{state.thread_id}|{state.request.request_id}|{validation.plan_id}|"
        f"{validation.status.value}|{validation.can_finalize}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"human-review-{digest}"


def build_product_human_review_request(state: ProductPlanningData) -> HumanReviewRequest:
    validation = (
        state.revision_result.validation
        if state.revision_result is not None
        else state.validation
    )
    if validation is None:
        raise ProductPlanningProtocolError("product review requires hard validation")
    kind = HumanReviewKind.PLAN_APPROVAL
    prompt = build_review_prompt(
        validation,
        approval_prompt="请确认是否批准这份多 Agent 行程草案, 或请求修改/取消。",
    )
    actions = (
        HumanReviewAction.APPROVE_DRAFT,
        HumanReviewAction.REQUEST_REVISION,
        HumanReviewAction.CANCEL,
    )
    if not validation.can_finalize:
        kind = HumanReviewKind.CONFLICT_RESOLUTION
        actions = (
            HumanReviewAction.ACKNOWLEDGE_CONFLICT,
            HumanReviewAction.REQUEST_REVISION,
            HumanReviewAction.CANCEL,
        )
    return HumanReviewRequest(
        review_id=_review_id(state),
        kind=kind,
        request_id=state.request.request_id,
        plan_id=validation.plan_id,
        prompt=prompt,
        allowed_actions=actions,
        validation_status=validation.status,
        can_finalize=validation.can_finalize,
        issue_rule_codes=tuple(item.rule_code for item in validation.issues),
    )


def validate_product_human_review_resume(
    review: HumanReviewRequest,
    resume: HumanReviewResume,
) -> None:
    if resume.review_id != review.review_id:
        raise ProductPlanningProtocolError("resume review_id does not match the product review")
    if resume.action not in review.allowed_actions:
        raise ProductPlanningProtocolError(
            f"action {resume.action.value} is not allowed for {review.kind.value}"
        )


def build_product_planning_graph(
    pipeline: ProductPlanningPipeline,
    checkpointer: BaseCheckpointSaver[str],
    *,
    clock: Clock = utc_now,
) -> CompiledStateGraph[
    ProductPlanningGraphState,
    None,
    ProductPlanningGraphState,
    ProductPlanningGraphState,
]:
    async def run_specialists_node(
        graph_state: ProductPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if state.status != PlanningThreadStatus.PLANNING or state.specialists is not None:
            raise ProductPlanningProtocolError("specialist node requires a fresh planning state")
        result = await pipeline.run_specialists(state.request, data_mode=state.data_mode)
        event = ProductPlanningEvent(
            node=ProductPlanningNodeName.RUN_SPECIALISTS,
            outcome=StatefulPlanningNodeOutcome.PLANNED,
            detail="Explore、Stay 与 Weather 分支已并行完成并保留独立结果。",
        )
        return {
            "state": _state_update(
                _evolve_state(state, specialists=result, events=(*state.events, event))
            )
        }

    async def build_materials_node(
        graph_state: ProductPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if state.specialists is None or state.materials is not None:
            raise ProductPlanningProtocolError("materials node requires specialist results")
        materials = await pipeline.build_materials(state.specialists)
        event = ProductPlanningEvent(
            node=ProductPlanningNodeName.BUILD_MATERIALS,
            outcome=StatefulPlanningNodeOutcome.PLANNED,
            detail="候选、住宿锚点、路线矩阵与预算目标已合并为可追溯规划材料。",
        )
        return {
            "state": _state_update(
                _evolve_state(state, materials=materials, events=(*state.events, event))
            )
        }

    def run_plan_agent_node(
        graph_state: ProductPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if state.materials is None or state.plan_agent is not None:
            raise ProductPlanningProtocolError("Plan Agent node requires planning materials")
        result = pipeline.run_plan(state.request, state.materials)
        if result.status != PlanAgentRunStatus.PLANNED or result.plan is None:
            raise ProductPlanningMaterialsBlockedError(state.materials.issues)
        plan = TripPlan.model_validate(
            result.plan.model_copy(update={"cost_items": state.cost_items}).model_dump(
                mode="python"
            )
        )
        event = ProductPlanningEvent(
            node=ProductPlanningNodeName.RUN_PLAN_AGENT,
            outcome=StatefulPlanningNodeOutcome.PLANNED,
            detail="Plan Agent 仅从已合并材料生成排程, 候选与路线事实由上游回填。",
        )
        return {
            "state": _state_update(
                _evolve_state(
                    state,
                    plan_agent=result,
                    plan=plan,
                    events=(*state.events, event),
                )
            )
        }

    def validate_hard_plan_node(
        graph_state: ProductPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if state.materials is None or state.plan is None or state.validation is not None:
            raise ProductPlanningProtocolError("hard validator requires a product plan")
        opening_hours = pipeline.build_opening_hours(
            state.request,
            state.plan,
            data_mode=state.data_mode,
        )
        validation = validate_hard_trip_plan(
            state.request,
            state.plan,
            state.materials,
            opening_hours,
        )
        event = ProductPlanningEvent(
            node=ProductPlanningNodeName.VALIDATE_HARD_PLAN,
            outcome=StatefulPlanningNodeOutcome.PLANNED,
            detail="Hard Validator 已检查约束、路线、营业证据与预算边界。",
        )
        return {
            "state": _state_update(
                _evolve_state(
                    state,
                    status=PlanningThreadStatus.PLAN_READY,
                    opening_hours=opening_hours,
                    validation=validation,
                    events=(*state.events, event),
                )
            )
        }

    async def run_repair_node(
        graph_state: ProductPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if (
            state.status != PlanningThreadStatus.PLAN_READY
            or state.materials is None
            or state.plan is None
            or state.opening_hours is None
            or state.validation is None
            or state.validation.can_finalize
            or state.repair is not None
        ):
            raise ProductPlanningProtocolError(
                "product repair requires a conflicted hard-validated plan"
            )
        repair = await run_repair_router(
            state.request,
            state.plan,
            state.materials,
            state.opening_hours,
            ProductRepairExecutor(pipeline),
        )
        event = ProductPlanningEvent(
            node=ProductPlanningNodeName.RUN_REPAIR,
            outcome=StatefulPlanningNodeOutcome.REVISED,
            detail=(
                f"Repair Router 完成 {len(repair.attempts)} 次有界尝试; "
                f"结果为 {repair.outcome.value}。"
            ),
        )
        return {
            "state": _state_update(
                _evolve_state(
                    state,
                    specialists=repair.final_materials.specialist_result,
                    materials=repair.final_materials,
                    plan=repair.final_plan,
                    opening_hours=repair.final_opening_hours,
                    validation=repair.final_report,
                    repair=repair,
                    events=(*state.events, event),
                )
            )
        }

    async def prepare_human_review_node(
        graph_state: ProductPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        effective_plan = (
            state.revision_result.revised_plan
            if state.revision_result is not None
            else state.plan
        )
        effective_materials = (
            state.revision_result.revised_materials
            if state.revision_result is not None
            and state.revision_result.revised_materials is not None
            else state.materials
        )
        if (
            state.status
            not in {PlanningThreadStatus.PLAN_READY, PlanningThreadStatus.REVISION_APPLIED}
            or effective_plan is None
            or effective_materials is None
        ):
            raise ProductPlanningProtocolError("product review requires a reviewable plan")
        weather_indoor_recovery = state.weather_indoor_recovery
        if weather_indoor_recovery is None:
            weather_indoor_recovery = await pipeline.recover_weather_indoor_candidates(
                state.request,
                effective_plan,
                effective_materials,
                state.data_mode,
            )
        review = build_product_human_review_request(state)
        event = ProductPlanningEvent(
            node=ProductPlanningNodeName.PREPARE_HUMAN_REVIEW,
            outcome=StatefulPlanningNodeOutcome.REVIEW_REQUIRED,
            detail=("已检查天气影响日的室内候选覆盖, 并生成可恢复的人审请求。"),
        )
        return {
            "state": _state_update(
                _evolve_state(
                    state,
                    status=PlanningThreadStatus.AWAITING_HUMAN_REVIEW,
                    review_request=review,
                    weather_indoor_recovery=weather_indoor_recovery,
                    events=(*state.events, event),
                )
            )
        }

    def human_review_node(
        graph_state: ProductPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if (
            state.status != PlanningThreadStatus.AWAITING_HUMAN_REVIEW
            or state.review_request is None
        ):
            raise ProductPlanningProtocolError("human review node requires a pending review")
        raw_resume = interrupt(state.review_request.model_dump(mode="json"))
        try:
            resume = HumanReviewResume.model_validate(raw_resume)
        except ValidationError as error:
            raise ProductPlanningProtocolError("invalid product review resume payload") from error
        validate_product_human_review_resume(state.review_request, resume)
        decision = HumanReviewDecision(
            **resume.model_dump(mode="python"),
            decided_at=clock(),
        )
        event = ProductPlanningEvent(
            node=ProductPlanningNodeName.HUMAN_REVIEW,
            outcome=StatefulPlanningNodeOutcome.RESUMED,
            detail="LangGraph interrupt 已由显式人审决定恢复。",
        )
        return {
            "state": _state_update(
                _evolve_state(
                    state,
                    status=PlanningThreadStatus.REVIEW_DECIDED,
                    review_decision=decision,
                    events=(*state.events, event),
                )
            )
        }

    def apply_review_decision_node(
        graph_state: ProductPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if state.status != PlanningThreadStatus.REVIEW_DECIDED or state.review_decision is None:
            raise ProductPlanningProtocolError("decision node requires review_decided status")
        status = {
            HumanReviewAction.APPROVE_DRAFT: PlanningThreadStatus.APPROVED_DRAFT,
            HumanReviewAction.ACKNOWLEDGE_CONFLICT: PlanningThreadStatus.CONFLICT_ACKNOWLEDGED,
            HumanReviewAction.REQUEST_REVISION: PlanningThreadStatus.REVISION_REQUESTED,
            HumanReviewAction.CANCEL: PlanningThreadStatus.CANCELLED,
        }[state.review_decision.action]
        event = ProductPlanningEvent(
            node=ProductPlanningNodeName.APPLY_REVIEW_DECISION,
            outcome=StatefulPlanningNodeOutcome.COMPLETED,
            detail=(
                "人审修改请求将进入受限 revision node。"
                if state.review_decision.action == HumanReviewAction.REQUEST_REVISION
                else "人审决定已映射为终态, 系统没有静默修改或放宽约束。"
            ),
        )
        return {
            "state": _state_update(
                _evolve_state(state, status=status, events=(*state.events, event))
            )
        }

    async def apply_plan_revision_node(
        graph_state: ProductPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        effective_plan = (
            state.revision_result.revised_plan
            if state.revision_result is not None
            else state.plan
        )
        effective_materials = (
            state.revision_result.revised_materials
            if state.revision_result is not None
            and state.revision_result.revised_materials is not None
            else state.materials
        )
        if (
            state.status != PlanningThreadStatus.REVISION_REQUESTED
            or state.review_decision is None
            or state.review_decision.revision_request is None
            or effective_plan is None
            or effective_materials is None
            or state.opening_hours is None
        ):
            raise ProductPlanningProtocolError("revision node requires persisted product inputs")
        revision_request = state.review_decision.revision_request
        if revision_request.operation.value == "replace_activity":
            revision = await apply_activity_replacement(
                state.request,
                effective_plan,
                effective_materials,
                revision_request,
                pipeline.get_revision_route,
                weather_indoor_recovery=state.weather_indoor_recovery,
            )
        else:
            revision = apply_plan_revision(
                state.request,
                effective_plan,
                revision_request,
            )
        validation_materials = revision.revised_materials or effective_materials
        hard_validation = validate_hard_trip_plan(
            state.request,
            revision.revised_plan,
            validation_materials,
            state.opening_hours,
        )
        revision = revision.model_copy(update={"validation": hard_validation})
        event = ProductPlanningEvent(
            node=ProductPlanningNodeName.APPLY_PLAN_REVISION,
            outcome=StatefulPlanningNodeOutcome.REVISED,
            detail=(
                "活动替换使用已记录的数据来源候选, 重算目标日路线、预算与 Hard Validator。"
                if revision_request.operation.value == "replace_activity"
                else "结构化修改复用上游结果, 并重新执行 Hard Validator。"
            ),
        )
        return {
            "state": _state_update(
                _evolve_state(
                    state,
                    status=PlanningThreadStatus.REVISION_APPLIED,
                    revision_result=revision,
                    events=(*state.events, event),
                )
            )
        }

    def route_after_review_decision(graph_state: ProductPlanningGraphState) -> str:
        state = _require_state(graph_state)
        if state.status == PlanningThreadStatus.REVISION_REQUESTED:
            return ProductPlanningNodeName.APPLY_PLAN_REVISION.value
        return END

    def route_after_hard_validation(graph_state: ProductPlanningGraphState) -> str:
        state = _require_state(graph_state)
        if state.validation is None:
            raise ProductPlanningProtocolError("hard validation route requires a report")
        if state.validation.can_finalize or should_skip_live_repair(
            state.data_mode,
            state.validation,
        ):
            return ProductPlanningNodeName.PREPARE_HUMAN_REVIEW.value
        return ProductPlanningNodeName.RUN_REPAIR.value

    workflow = StateGraph(ProductPlanningGraphState)
    nodes: tuple[tuple[ProductPlanningNodeName, object], ...] = (
        (ProductPlanningNodeName.RUN_SPECIALISTS, run_specialists_node),
        (ProductPlanningNodeName.BUILD_MATERIALS, build_materials_node),
        (ProductPlanningNodeName.RUN_PLAN_AGENT, run_plan_agent_node),
        (ProductPlanningNodeName.VALIDATE_HARD_PLAN, validate_hard_plan_node),
        (ProductPlanningNodeName.RUN_REPAIR, run_repair_node),
        (ProductPlanningNodeName.PREPARE_HUMAN_REVIEW, prepare_human_review_node),
        (ProductPlanningNodeName.HUMAN_REVIEW, human_review_node),
        (ProductPlanningNodeName.APPLY_REVIEW_DECISION, apply_review_decision_node),
        (ProductPlanningNodeName.APPLY_PLAN_REVISION, apply_plan_revision_node),
    )
    for name, node in nodes:
        workflow.add_node(name.value, cast(Any, node))
    workflow.add_edge(START, ProductPlanningNodeName.RUN_SPECIALISTS.value)
    workflow.add_edge(
        ProductPlanningNodeName.RUN_SPECIALISTS.value,
        ProductPlanningNodeName.BUILD_MATERIALS.value,
    )
    workflow.add_edge(
        ProductPlanningNodeName.BUILD_MATERIALS.value,
        ProductPlanningNodeName.RUN_PLAN_AGENT.value,
    )
    workflow.add_edge(
        ProductPlanningNodeName.RUN_PLAN_AGENT.value,
        ProductPlanningNodeName.VALIDATE_HARD_PLAN.value,
    )
    workflow.add_conditional_edges(
        ProductPlanningNodeName.VALIDATE_HARD_PLAN.value,
        route_after_hard_validation,
        {
            ProductPlanningNodeName.RUN_REPAIR.value: ProductPlanningNodeName.RUN_REPAIR.value,
            ProductPlanningNodeName.PREPARE_HUMAN_REVIEW.value: (
                ProductPlanningNodeName.PREPARE_HUMAN_REVIEW.value
            ),
        },
    )
    workflow.add_edge(
        ProductPlanningNodeName.RUN_REPAIR.value,
        ProductPlanningNodeName.PREPARE_HUMAN_REVIEW.value,
    )
    workflow.add_edge(
        ProductPlanningNodeName.PREPARE_HUMAN_REVIEW.value,
        ProductPlanningNodeName.HUMAN_REVIEW.value,
    )
    workflow.add_edge(
        ProductPlanningNodeName.HUMAN_REVIEW.value,
        ProductPlanningNodeName.APPLY_REVIEW_DECISION.value,
    )
    workflow.add_conditional_edges(
        ProductPlanningNodeName.APPLY_REVIEW_DECISION.value,
        route_after_review_decision,
        {
            ProductPlanningNodeName.APPLY_PLAN_REVISION.value: (
                ProductPlanningNodeName.APPLY_PLAN_REVISION.value
            ),
            END: END,
        },
    )
    workflow.add_edge(
        ProductPlanningNodeName.APPLY_PLAN_REVISION.value,
        ProductPlanningNodeName.PREPARE_HUMAN_REVIEW.value,
    )
    return workflow.compile(checkpointer=checkpointer, name=PRODUCT_PLANNING_GRAPH_NAME)


def build_product_run_config(
    thread_id: str,
    *,
    request_id: str | None = None,
) -> RunnableConfig:
    metadata: dict[str, object] = {
        "workflow_version": "product-planning-graph-v2",
        "raw_user_text_in_metadata": False,
    }
    if request_id is not None:
        metadata["request_id"] = request_id
    return {
        "run_name": PRODUCT_PLANNING_GRAPH_NAME,
        "tags": ["ez-405b", "product-graph-v2", "bounded-repair", "multi-agent", "hitl"],
        "metadata": metadata,
        "configurable": {"thread_id": thread_id},
    }


def _to_snapshot(snapshot: StateSnapshot) -> ProductPlanningSnapshot:
    values = cast(dict[str, object], snapshot.values)
    raw_state = values.get("state")
    if raw_state is None:
        raise ProductPlanningProtocolError("checkpoint contains no product planning state")
    state = ProductPlanningData.model_validate(raw_state)
    checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise ProductPlanningProtocolError("product checkpoint has no checkpoint_id")
    return ProductPlanningSnapshot(
        thread_id=state.thread_id,
        checkpoint_id=checkpoint_id,
        next_nodes=tuple(str(node) for node in snapshot.next),
        state=state,
    )


class ProductPlanningRuntime:
    def __init__(
        self,
        pipeline: ProductPlanningPipeline,
        checkpointer: BaseCheckpointSaver[str],
        *,
        clock: Clock = utc_now,
    ) -> None:
        self.graph = build_product_planning_graph(pipeline, checkpointer, clock=clock)

    async def _stream(
        self,
        input_value: ProductPlanningGraphState | Command[Any],
        config: RunnableConfig,
        on_progress: ProductProgressCallback | None,
    ) -> None:
        async for raw_update in self.graph.astream(
            input_value,
            config=config,
            stream_mode="updates",
        ):
            if not isinstance(raw_update, dict):
                raise ProductPlanningProtocolError("product update stream returned invalid data")
            for raw_node, raw_payload in raw_update.items():
                if raw_node == "__interrupt__":
                    continue
                try:
                    node = ProductPlanningNodeName(str(raw_node))
                except ValueError as error:
                    raise ProductPlanningProtocolError(
                        f"product update stream returned unknown node {raw_node}"
                    ) from error
                if not isinstance(raw_payload, dict) or raw_payload.get("state") is None:
                    raise ProductPlanningProtocolError(
                        f"product update for {node.value} contains no committed state"
                    )
                state = ProductPlanningData.model_validate(raw_payload["state"])
                if not state.events or state.events[-1].node != node:
                    raise ProductPlanningProtocolError(
                        f"product update for {node.value} does not match its state event"
                    )
                if on_progress is not None:
                    await on_progress(
                        ProductPlanningProgress(
                            node=node,
                            state_status=state.status,
                            event=state.events[-1],
                        )
                    )

    async def start_with_progress(
        self,
        thread_id: str,
        request: TripRequest,
        cost_items: tuple[CostItem, ...],
        *,
        data_mode: DataMode,
        on_progress: ProductProgressCallback | None = None,
    ) -> ProductPlanningSnapshot:
        config = build_product_run_config(thread_id, request_id=request.request_id)
        if (await self.graph.aget_state(config)).values:
            raise DuplicateProductPlanningThreadError(
                f"product planning thread {thread_id} already exists"
            )
        initial = ProductPlanningData(
            thread_id=thread_id,
            request=request,
            cost_items=cost_items,
            data_mode=data_mode,
        )
        await self._stream(
            ProductPlanningGraphState(state=_state_update(initial)),
            config,
            on_progress,
        )
        return await self.snapshot(thread_id)

    async def resume_with_progress(
        self,
        thread_id: str,
        resume: HumanReviewResume,
        *,
        on_progress: ProductProgressCallback | None = None,
    ) -> ProductPlanningSnapshot:
        pending = await self.snapshot(thread_id)
        if (
            pending.state.status != PlanningThreadStatus.AWAITING_HUMAN_REVIEW
            or pending.state.review_request is None
        ):
            raise ProductPlanningProtocolError("product thread is not awaiting human review")
        validate_product_human_review_resume(pending.state.review_request, resume)
        config = build_product_run_config(thread_id)
        await self._stream(
            Command(resume=resume.model_dump(mode="json")),
            config,
            on_progress,
        )
        return await self.snapshot(thread_id)

    async def snapshot(self, thread_id: str) -> ProductPlanningSnapshot:
        snapshot = await self.graph.aget_state(build_product_run_config(thread_id))
        if not snapshot.values:
            raise ProductPlanningProtocolError(f"product thread {thread_id} does not exist")
        return _to_snapshot(snapshot)


@asynccontextmanager
async def open_sqlite_product_runtime(
    checkpoint_path: Path,
    pipeline: ProductPlanningPipeline,
    *,
    clock: Clock = utc_now,
) -> AsyncIterator[ProductPlanningRuntime]:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        yield ProductPlanningRuntime(pipeline, checkpointer, clock=clock)
