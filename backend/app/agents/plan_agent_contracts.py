from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.agents.contracts import ModelTokenUsage, PlannerPlacementProposal
from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.planning import ActivityKind, ItineraryItem, TripPlan
from app.domain.validation import PlanValidationReport
from app.planning.material_contracts import (
    BudgetAllocationStatus,
    PlanningMaterialStatus,
)


class PlanAgentRunStatus(StrEnum):
    PLANNED = "planned"
    SKIPPED = "skipped"


class PlanAgentSkipReason(StrEnum):
    MATERIALS_NOT_READY = "materials_not_ready"


class PlanAgentDecision(DomainModel):
    proposal: PlannerPlacementProposal
    item: ItineraryItem
    route_edge_id: Identifier

    @model_validator(mode="after")
    def validate_grounded_decision(self) -> "PlanAgentDecision":
        if self.item.candidate_id != self.proposal.candidate_id:
            raise ValueError("Plan Agent decision must preserve the proposed candidate_id")
        if self.item.kind not in {ActivityKind.ATTRACTION, ActivityKind.MEAL}:
            raise ValueError("Plan Agent decisions can only create POI activity items")
        if self.item.route_from_previous is None:
            raise ValueError("ready planning materials require a grounded incoming route")
        return self


class PlanAgentRunResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    agent_version: Literal["multi-agent-plan-v1"] = "multi-agent-plan-v1"
    prompt_version: Literal["route-weather-budget-schedule-v1"] = "route-weather-budget-schedule-v1"
    request_id: Identifier
    context_id: Identifier
    input_material_sha256: Sha256Digest
    material_status: PlanningMaterialStatus
    budget_status: BudgetAllocationStatus
    status: PlanAgentRunStatus
    skip_reason: PlanAgentSkipReason | None = None
    input_poi_candidate_ids: tuple[Identifier, ...] = Field(max_length=4)
    primary_stay_candidate_id: Identifier | None = None
    input_route_edge_count: int = Field(ge=0, le=20)
    input_weather_risk_ids: tuple[Identifier, ...] = ()
    model: NonEmptyText | None = None
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None
    model_call_count: int = Field(ge=0, le=1)
    decisions: tuple[PlanAgentDecision, ...] = Field(default=(), max_length=4)
    route_edge_ids_used: tuple[Identifier, ...] = Field(default=(), max_length=4)
    plan: TripPlan | None = None
    validation: PlanValidationReport | None = None

    @model_validator(mode="after")
    def validate_run_boundary(self) -> "PlanAgentRunResult":
        if len(self.input_poi_candidate_ids) != len(set(self.input_poi_candidate_ids)):
            raise ValueError("Plan Agent input POI ids must be unique")
        if len(self.input_weather_risk_ids) != len(set(self.input_weather_risk_ids)):
            raise ValueError("Plan Agent input weather risk ids must be unique")

        if self.status == PlanAgentRunStatus.SKIPPED:
            if self.material_status == PlanningMaterialStatus.READY:
                raise ValueError("ready materials cannot produce a skipped Plan Agent result")
            if (
                self.skip_reason != PlanAgentSkipReason.MATERIALS_NOT_READY
                or self.model is not None
                or self.latency_ms
                or self.usage is not None
                or self.model_call_count
                or self.decisions
                or self.route_edge_ids_used
                or self.plan is not None
                or self.validation is not None
            ):
                raise ValueError("skipped Plan Agent results cannot contain model or plan output")
            return self

        if self.material_status != PlanningMaterialStatus.READY:
            raise ValueError("Plan Agent may only plan from ready materials")
        if (
            self.skip_reason is not None
            or self.model is None
            or self.model_call_count != 1
            or self.plan is None
            or self.validation is None
        ):
            raise ValueError("planned results require exactly one model call and validated plan")
        if self.primary_stay_candidate_id is None:
            raise ValueError("planned results require the stay route anchor")

        decision_ids = tuple(item.proposal.candidate_id for item in self.decisions)
        if len(decision_ids) != len(set(decision_ids)) or set(decision_ids) != set(
            self.input_poi_candidate_ids
        ):
            raise ValueError("Plan Agent decisions must cover each input POI exactly once")
        used_edges = tuple(item.route_edge_id for item in self.decisions)
        if self.route_edge_ids_used != used_edges or len(used_edges) != len(set(used_edges)):
            raise ValueError("route edge lineage must match the Plan Agent decisions")

        assert self.plan is not None
        assert self.validation is not None
        if (
            self.plan.request_id != self.request_id
            or self.validation.request_id != self.request_id
            or self.validation.plan_id != self.plan.plan_id
        ):
            raise ValueError("Plan Agent stages must preserve request and plan identity")
        scheduled_items = tuple(
            item
            for day in self.plan.days
            for item in day.items
            if item.kind in {ActivityKind.ATTRACTION, ActivityKind.MEAL}
        )
        if scheduled_items != tuple(item.item for item in self.decisions):
            raise ValueError("TripPlan must preserve Plan Agent decisions in timeline order")
        if self.plan.cost_items:
            raise ValueError("budget targets are not verified prices and cannot become CostItems")
        if tuple(item.risk_id for item in self.plan.weather_risks) != self.input_weather_risk_ids:
            raise ValueError("TripPlan must preserve the exact weather specialist output")
        for day in self.plan.days:
            expected_risk_ids = tuple(
                risk.risk_id
                for risk in self.plan.weather_risks
                if risk.starts_at.date() <= day.date <= risk.ends_at.date()
            )
            if day.weather_risk_ids != expected_risk_ids:
                raise ValueError("each day must reference exactly its overlapping weather risks")
        return self
