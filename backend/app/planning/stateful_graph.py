import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt
from pydantic import ValidationError

from app.agents.single_planner import PlannerProposalModel
from app.domain.money import CostItem
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.planning.stateful_contracts import (
    CheckpointHistoryEntry,
    Clock,
    HumanReviewAction,
    HumanReviewDecision,
    HumanReviewKind,
    HumanReviewRequest,
    HumanReviewResume,
    PlanningThreadStatus,
    ProgressCallback,
    StatefulPlanningData,
    StatefulPlanningEvent,
    StatefulPlanningNodeName,
    StatefulPlanningNodeOutcome,
    StatefulPlanningProgress,
    StatefulPlanningSnapshot,
    utc_now,
)
from app.planning.vertical_slice import run_trip_planning_vertical_slice
from app.providers.ports import TravelDataProvider

STATEFUL_PLANNING_GRAPH_NAME = "eztrip-stateful-planning-checkpoint-v1"


class StatefulPlanningProtocolError(RuntimeError):
    """Raised when a persisted planning thread violates the workflow protocol."""


class DuplicatePlanningThreadError(StatefulPlanningProtocolError):
    """Raised when a caller tries to replace an existing checkpoint thread."""


class StatefulPlanningGraphState(TypedDict):
    state: dict[str, object]


def _require_state(graph_state: StatefulPlanningGraphState) -> StatefulPlanningData:
    raw_state = graph_state.get("state")
    if raw_state is None:
        raise StatefulPlanningProtocolError("stateful planning node received no state")
    try:
        return StatefulPlanningData.model_validate(raw_state)
    except ValidationError as error:
        raise StatefulPlanningProtocolError("checkpoint planning state is invalid") from error


def _state_update(state: StatefulPlanningData) -> dict[str, object]:
    return state.model_dump(mode="json")


def _evolve_state(current: StatefulPlanningData, **updates: object) -> StatefulPlanningData:
    payload = current.model_dump(mode="python")
    payload.update(updates)
    return StatefulPlanningData.model_validate(payload)


def _review_id(state: StatefulPlanningData) -> str:
    assert state.vertical_slice is not None
    validation = state.vertical_slice.validation
    material = (
        f"{state.thread_id}|{state.request.request_id}|{validation.plan_id}|"
        f"{validation.status.value}|{validation.can_finalize}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"human-review-{digest}"


def build_human_review_request(state: StatefulPlanningData) -> HumanReviewRequest:
    result = state.vertical_slice
    if result is None:
        raise StatefulPlanningProtocolError("human review requires a vertical slice result")
    validation = result.validation
    kind = HumanReviewKind.PLAN_APPROVAL
    prompt = "请确认是否批准这份 provider-grounded 行程草案, 或请求修改/取消。"
    actions = (
        HumanReviewAction.APPROVE_DRAFT,
        HumanReviewAction.REQUEST_REVISION,
        HumanReviewAction.CANCEL,
    )
    if not validation.can_finalize:
        kind = HumanReviewKind.CONFLICT_RESOLUTION
        prompt = "当前草案存在硬冲突, 请确认已知晓、请求修改或取消; 系统不会自动放宽约束。"
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


def validate_human_review_resume(
    review: HumanReviewRequest,
    resume: HumanReviewResume,
) -> None:
    if resume.review_id != review.review_id:
        raise StatefulPlanningProtocolError("resume review_id does not match the pending review")
    if resume.action not in review.allowed_actions:
        raise StatefulPlanningProtocolError(
            f"action {resume.action.value} is not allowed for {review.kind.value}"
        )


def build_stateful_planning_graph(
    provider: TravelDataProvider,
    planner_model: PlannerProposalModel,
    checkpointer: BaseCheckpointSaver[str],
    *,
    clock: Clock = utc_now,
) -> CompiledStateGraph[
    StatefulPlanningGraphState,
    None,
    StatefulPlanningGraphState,
    StatefulPlanningGraphState,
]:
    async def run_vertical_slice_node(
        graph_state: StatefulPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if state.status != PlanningThreadStatus.PLANNING:
            raise StatefulPlanningProtocolError("planning node requires planning status")
        result = await run_trip_planning_vertical_slice(
            state.request,
            provider,
            planner_model,
            state.cost_items,
            data_mode=state.data_mode,
        )
        event = StatefulPlanningEvent(
            node=StatefulPlanningNodeName.RUN_VERTICAL_SLICE,
            outcome=StatefulPlanningNodeOutcome.PLANNED,
            detail="现有 Gate 2 纵向切片已生成并完成确定性校验。",
        )
        return {
            "state": _state_update(
                _evolve_state(
                    state,
                    status=PlanningThreadStatus.PLAN_READY,
                    vertical_slice=result,
                    events=(*state.events, event),
                )
            )
        }

    def prepare_human_review_node(
        graph_state: StatefulPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if state.status != PlanningThreadStatus.PLAN_READY:
            raise StatefulPlanningProtocolError("review preparation requires plan_ready status")
        review = build_human_review_request(state)
        event = StatefulPlanningEvent(
            node=StatefulPlanningNodeName.PREPARE_HUMAN_REVIEW,
            outcome=StatefulPlanningNodeOutcome.REVIEW_REQUIRED,
            detail="已按 validation 结果生成可恢复的人审请求。",
        )
        return {
            "state": _state_update(
                _evolve_state(
                    state,
                    status=PlanningThreadStatus.AWAITING_HUMAN_REVIEW,
                    review_request=review,
                    events=(*state.events, event),
                )
            )
        }

    def human_review_node(
        graph_state: StatefulPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if (
            state.status != PlanningThreadStatus.AWAITING_HUMAN_REVIEW
            or state.review_request is None
        ):
            raise StatefulPlanningProtocolError("human review node requires a pending review")
        raw_resume = interrupt(state.review_request.model_dump(mode="json"))
        try:
            resume = HumanReviewResume.model_validate(raw_resume)
        except ValidationError as error:
            raise StatefulPlanningProtocolError("invalid human review resume payload") from error
        validate_human_review_resume(state.review_request, resume)
        decision = HumanReviewDecision(
            **resume.model_dump(mode="python"),
            decided_at=clock(),
        )
        event = StatefulPlanningEvent(
            node=StatefulPlanningNodeName.HUMAN_REVIEW,
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
        graph_state: StatefulPlanningGraphState,
    ) -> dict[str, Any]:
        state = _require_state(graph_state)
        if state.status != PlanningThreadStatus.REVIEW_DECIDED or state.review_decision is None:
            raise StatefulPlanningProtocolError("decision node requires review_decided status")
        status = {
            HumanReviewAction.APPROVE_DRAFT: PlanningThreadStatus.APPROVED_DRAFT,
            HumanReviewAction.ACKNOWLEDGE_CONFLICT: (PlanningThreadStatus.CONFLICT_ACKNOWLEDGED),
            HumanReviewAction.REQUEST_REVISION: PlanningThreadStatus.REVISION_REQUESTED,
            HumanReviewAction.CANCEL: PlanningThreadStatus.CANCELLED,
        }[state.review_decision.action]
        event = StatefulPlanningEvent(
            node=StatefulPlanningNodeName.APPLY_REVIEW_DECISION,
            outcome=StatefulPlanningNodeOutcome.COMPLETED,
            detail="人审决定已映射为终态, 原 TripPlan 保持 draft 且未被静默修改。",
        )
        return {
            "state": _state_update(
                _evolve_state(
                    state,
                    status=status,
                    events=(*state.events, event),
                )
            )
        }

    workflow = StateGraph(StatefulPlanningGraphState)
    # LangGraph's overloaded add_node type cannot infer this single-field state,
    # while every node remains explicitly typed and checked above.
    workflow.add_node(
        StatefulPlanningNodeName.RUN_VERTICAL_SLICE.value,
        cast(Any, run_vertical_slice_node),
    )
    workflow.add_node(
        StatefulPlanningNodeName.PREPARE_HUMAN_REVIEW.value,
        cast(Any, prepare_human_review_node),
    )
    workflow.add_node(
        StatefulPlanningNodeName.HUMAN_REVIEW.value,
        cast(Any, human_review_node),
    )
    workflow.add_node(
        StatefulPlanningNodeName.APPLY_REVIEW_DECISION.value,
        cast(Any, apply_review_decision_node),
    )
    workflow.add_edge(START, StatefulPlanningNodeName.RUN_VERTICAL_SLICE.value)
    workflow.add_edge(
        StatefulPlanningNodeName.RUN_VERTICAL_SLICE.value,
        StatefulPlanningNodeName.PREPARE_HUMAN_REVIEW.value,
    )
    workflow.add_edge(
        StatefulPlanningNodeName.PREPARE_HUMAN_REVIEW.value,
        StatefulPlanningNodeName.HUMAN_REVIEW.value,
    )
    workflow.add_edge(
        StatefulPlanningNodeName.HUMAN_REVIEW.value,
        StatefulPlanningNodeName.APPLY_REVIEW_DECISION.value,
    )
    workflow.add_edge(StatefulPlanningNodeName.APPLY_REVIEW_DECISION.value, END)
    return workflow.compile(checkpointer=checkpointer, name=STATEFUL_PLANNING_GRAPH_NAME)


def build_stateful_run_config(
    thread_id: str,
    *,
    request_id: str | None = None,
) -> RunnableConfig:
    metadata: dict[str, object] = {
        "workflow_version": "stateful-planning-checkpoint-v1",
        "raw_user_text_in_metadata": False,
    }
    if request_id is not None:
        metadata["request_id"] = request_id
    return {
        "run_name": STATEFUL_PLANNING_GRAPH_NAME,
        "tags": ["ez-201", "checkpoint", "hitl"],
        "metadata": metadata,
        "configurable": {"thread_id": thread_id},
    }


def _snapshot_state(snapshot: StateSnapshot) -> StatefulPlanningData:
    values = cast(dict[str, object], snapshot.values)
    raw_state = values.get("state")
    if raw_state is None:
        raise StatefulPlanningProtocolError("checkpoint contains no planning state")
    return StatefulPlanningData.model_validate(raw_state)


def _checkpoint_id(snapshot: StateSnapshot) -> str:
    configurable = snapshot.config.get("configurable", {})
    checkpoint_id = configurable.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise StatefulPlanningProtocolError("checkpoint has no checkpoint_id")
    return checkpoint_id


def _to_snapshot(snapshot: StateSnapshot) -> StatefulPlanningSnapshot:
    state = _snapshot_state(snapshot)
    return StatefulPlanningSnapshot(
        thread_id=state.thread_id,
        checkpoint_id=_checkpoint_id(snapshot),
        next_nodes=tuple(str(node) for node in snapshot.next),
        state=state,
    )


class StatefulPlanningRuntime:
    def __init__(
        self,
        provider: TravelDataProvider,
        planner_model: PlannerProposalModel,
        checkpointer: BaseCheckpointSaver[str],
        *,
        clock: Clock = utc_now,
    ) -> None:
        self.graph = build_stateful_planning_graph(
            provider,
            planner_model,
            checkpointer,
            clock=clock,
        )

    async def start(
        self,
        thread_id: str,
        request: TripRequest,
        cost_items: tuple[CostItem, ...],
        *,
        data_mode: DataMode,
    ) -> StatefulPlanningSnapshot:
        return await self.start_with_progress(
            thread_id,
            request,
            cost_items,
            data_mode=data_mode,
        )

    async def start_with_progress(
        self,
        thread_id: str,
        request: TripRequest,
        cost_items: tuple[CostItem, ...],
        *,
        data_mode: DataMode,
        on_progress: ProgressCallback | None = None,
    ) -> StatefulPlanningSnapshot:
        config = build_stateful_run_config(thread_id, request_id=request.request_id)
        existing = await self.graph.aget_state(config)
        if existing.values:
            raise DuplicatePlanningThreadError(
                f"planning thread {thread_id} already has checkpoint state"
            )
        initial = StatefulPlanningData(
            thread_id=thread_id,
            request=request,
            cost_items=cost_items,
            data_mode=data_mode,
        )
        async for raw_update in self.graph.astream(
            {"state": _state_update(initial)},
            config=config,
            stream_mode="updates",
        ):
            if not isinstance(raw_update, dict):
                raise StatefulPlanningProtocolError("planning update stream returned invalid data")
            for raw_node, raw_payload in raw_update.items():
                if raw_node == "__interrupt__":
                    continue
                try:
                    node = StatefulPlanningNodeName(str(raw_node))
                except ValueError as error:
                    raise StatefulPlanningProtocolError(
                        f"planning update stream returned unknown node {raw_node}"
                    ) from error
                if not isinstance(raw_payload, dict):
                    raise StatefulPlanningProtocolError(
                        f"planning update for {node.value} contains no state payload"
                    )
                raw_state = raw_payload.get("state")
                if raw_state is None:
                    raise StatefulPlanningProtocolError(
                        f"planning update for {node.value} contains no committed state"
                    )
                state = StatefulPlanningData.model_validate(raw_state)
                if not state.events or state.events[-1].node != node:
                    raise StatefulPlanningProtocolError(
                        f"planning update for {node.value} does not match its state event"
                    )
                if on_progress is not None:
                    await on_progress(
                        StatefulPlanningProgress(
                            node=node,
                            state_status=state.status,
                            event=state.events[-1],
                        )
                    )
        return await self.snapshot(thread_id)

    async def resume(
        self,
        thread_id: str,
        resume: HumanReviewResume,
    ) -> StatefulPlanningSnapshot:
        return await self.resume_with_progress(thread_id, resume)

    async def resume_with_progress(
        self,
        thread_id: str,
        resume: HumanReviewResume,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> StatefulPlanningSnapshot:
        pending = await self.snapshot(thread_id)
        if (
            pending.state.status != PlanningThreadStatus.AWAITING_HUMAN_REVIEW
            or pending.state.review_request is None
        ):
            raise StatefulPlanningProtocolError("planning thread is not awaiting human review")
        validate_human_review_resume(pending.state.review_request, resume)
        config = build_stateful_run_config(thread_id)
        async for raw_update in self.graph.astream(
            Command(resume=resume.model_dump(mode="json")),
            config=config,
            stream_mode="updates",
        ):
            if not isinstance(raw_update, dict):
                raise StatefulPlanningProtocolError("planning update stream returned invalid data")
            for raw_node, raw_payload in raw_update.items():
                if raw_node == "__interrupt__":
                    continue
                try:
                    node = StatefulPlanningNodeName(str(raw_node))
                except ValueError as error:
                    raise StatefulPlanningProtocolError(
                        f"planning update stream returned unknown node {raw_node}"
                    ) from error
                if not isinstance(raw_payload, dict):
                    raise StatefulPlanningProtocolError(
                        f"planning update for {node.value} contains no state payload"
                    )
                raw_state = raw_payload.get("state")
                if raw_state is None:
                    raise StatefulPlanningProtocolError(
                        f"planning update for {node.value} contains no committed state"
                    )
                state = StatefulPlanningData.model_validate(raw_state)
                if not state.events or state.events[-1].node != node:
                    raise StatefulPlanningProtocolError(
                        f"planning update for {node.value} does not match its state event"
                    )
                if on_progress is not None:
                    await on_progress(
                        StatefulPlanningProgress(
                            node=node,
                            state_status=state.status,
                            event=state.events[-1],
                        )
                    )
        return await self.snapshot(thread_id)

    async def snapshot(self, thread_id: str) -> StatefulPlanningSnapshot:
        config = build_stateful_run_config(thread_id)
        snapshot = await self.graph.aget_state(config)
        if not snapshot.values:
            raise StatefulPlanningProtocolError(f"planning thread {thread_id} does not exist")
        return _to_snapshot(snapshot)

    async def history(self, thread_id: str) -> tuple[CheckpointHistoryEntry, ...]:
        config = build_stateful_run_config(thread_id)
        entries: list[CheckpointHistoryEntry] = []
        async for snapshot in self.graph.aget_state_history(config):
            metadata = snapshot.metadata or {}
            writes = metadata.get("writes")
            write_nodes: tuple[str, ...] = ()
            if isinstance(writes, dict):
                write_nodes = tuple(str(name) for name in writes)
            status = None
            if snapshot.values:
                status = _snapshot_state(snapshot).status
            entries.append(
                CheckpointHistoryEntry(
                    step=int(metadata.get("step", -1)),
                    source=str(metadata.get("source", "unknown")),
                    write_nodes=write_nodes,
                    next_nodes=tuple(str(node) for node in snapshot.next),
                    state_status=status,
                )
            )
        if not entries:
            raise StatefulPlanningProtocolError(f"planning thread {thread_id} has no history")
        return tuple(sorted(entries, key=lambda item: item.step))


@asynccontextmanager
async def open_sqlite_planning_runtime(
    checkpoint_path: Path,
    provider: TravelDataProvider,
    planner_model: PlannerProposalModel,
    *,
    clock: Clock = utc_now,
) -> AsyncIterator[StatefulPlanningRuntime]:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        yield StatefulPlanningRuntime(
            provider,
            planner_model,
            checkpointer,
            clock=clock,
        )
