import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import model_validator

from app.agents.contracts import SinglePlannerAgentResult
from app.agents.single_planner import PlannerProposalModel, run_single_planner
from app.domain.base import DomainModel, Identifier
from app.domain.money import CostItem
from app.domain.planning import PlanStatus, TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.validation import PlanValidationReport, PlanValidationStatus
from app.domain.workflow import MinimalPlanningResult, PlanningWorkflowStatus
from app.planning.minimal_graph import run_minimal_planning_graph
from app.planning.validator import validate_trip_plan
from app.providers.ports import TravelDataProvider

VERTICAL_SLICE_VERSION = "beijing-three-day-vertical-slice-v1"


class VerticalSliceProtocolError(RuntimeError):
    """Raised when one completed stage cannot safely feed the next stage."""


class VerticalSliceOutcome(StrEnum):
    READY = "ready"
    CONFLICTED = "conflicted"


class VerticalSliceResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    workflow_version: Literal["beijing-three-day-vertical-slice-v1"] = (
        "beijing-three-day-vertical-slice-v1"
    )
    request_id: Identifier
    data_mode: Literal[DataMode.LIVE, DataMode.FIXTURE]
    outcome: VerticalSliceOutcome
    upstream: MinimalPlanningResult
    planner: SinglePlannerAgentResult
    plan: TripPlan
    validation: PlanValidationReport

    @model_validator(mode="after")
    def validate_stage_lineage(self) -> "VerticalSliceResult":
        request_ids = {
            self.request_id,
            self.upstream.request_id,
            self.planner.request_id,
            self.plan.request_id,
            self.validation.request_id,
        }
        if len(request_ids) != 1:
            raise ValueError("vertical slice stages must preserve one request_id")
        if self.upstream.data_mode != self.data_mode:
            raise ValueError("vertical slice data_mode must match the upstream result")
        if self.upstream.status != PlanningWorkflowStatus.CANDIDATES_READY:
            raise ValueError("vertical slice requires a candidates_ready upstream result")
        if self.planner.context_id != self.upstream.planner_context.context_id:
            raise ValueError("Planner result must preserve the upstream context_id")
        if self.plan.plan_id != self.validation.plan_id:
            raise ValueError("validation must reference the assembled plan")
        expected_outcome = VerticalSliceOutcome.READY
        if self.validation.status == PlanValidationStatus.CONFLICTED:
            expected_outcome = VerticalSliceOutcome.CONFLICTED
        if self.outcome != expected_outcome:
            raise ValueError("vertical slice outcome must follow validation status")
        if self.plan.status == PlanStatus.FINAL:
            raise ValueError("Gate 2 vertical slice cannot auto-finalize a plan")
        return self


def _plan_id(
    request: TripRequest,
    planner: SinglePlannerAgentResult,
    cost_items: tuple[CostItem, ...],
) -> str:
    payload = {
        "request_id": request.request_id,
        "context_id": planner.context_id,
        "candidate_set": planner.input_candidates_sha256,
        "day_plans": [day.model_dump(mode="json") for day in planner.day_plans],
        "cost_items": [item.model_dump(mode="json") for item in cost_items],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"trip-plan-{digest}"


def assemble_trip_plan(
    request: TripRequest,
    upstream: MinimalPlanningResult,
    planner: SinglePlannerAgentResult,
    cost_items: tuple[CostItem, ...],
) -> TripPlan:
    if upstream.status != PlanningWorkflowStatus.CANDIDATES_READY:
        raise VerticalSliceProtocolError("trip plan assembly requires candidates_ready upstream")
    if request.request_id != upstream.request_id or planner.request_id != request.request_id:
        raise VerticalSliceProtocolError("trip plan assembly received mismatched request ids")
    if planner.context_id != upstream.planner_context.context_id:
        raise VerticalSliceProtocolError("trip plan assembly received a mismatched Planner context")

    expected_dates = tuple(day.date for day in upstream.planner_context.days)
    actual_dates = tuple(day.date for day in planner.day_plans)
    if actual_dates != expected_dates:
        raise VerticalSliceProtocolError(
            "Gate 2 assembly requires one non-empty DayPlan for every trip date"
        )

    upstream_candidate_ids = {item.candidate_id for item in upstream.candidates}
    scheduled_candidate_ids = {
        item.candidate_id
        for day in planner.day_plans
        for item in day.items
        if item.candidate_id is not None
    }
    if scheduled_candidate_ids != upstream_candidate_ids:
        raise VerticalSliceProtocolError(
            "assembled plan must preserve the complete provider candidate set"
        )

    return TripPlan(
        plan_id=_plan_id(request, planner, cost_items),
        request_id=request.request_id,
        status=PlanStatus.DRAFT,
        destination_city=upstream.planner_context.destination.normalized_name,
        start_date=request.start_date,
        end_date=request.end_date,
        days=planner.day_plans,
        cost_items=cost_items,
    )


async def run_trip_planning_vertical_slice(
    request: TripRequest,
    provider: TravelDataProvider,
    planner_model: PlannerProposalModel,
    cost_items: tuple[CostItem, ...],
    *,
    data_mode: DataMode,
) -> VerticalSliceResult:
    upstream = await run_minimal_planning_graph(request, provider, data_mode=data_mode)
    if upstream.status != PlanningWorkflowStatus.CANDIDATES_READY:
        raise VerticalSliceProtocolError(
            f"vertical slice stopped before Planner: upstream status={upstream.status.value}"
        )
    planner = run_single_planner(
        upstream.planner_context,
        upstream.candidates,
        planner_model,
    )
    plan = assemble_trip_plan(request, upstream, planner, cost_items)
    validation = validate_trip_plan(request, plan)
    outcome = VerticalSliceOutcome.READY
    if validation.status == PlanValidationStatus.CONFLICTED:
        outcome = VerticalSliceOutcome.CONFLICTED
    return VerticalSliceResult(
        request_id=request.request_id,
        data_mode=data_mode,
        outcome=outcome,
        upstream=upstream,
        planner=planner,
        plan=plan,
        validation=validation,
    )
