from enum import StrEnum
from typing import Literal

from pydantic import model_validator

from app.agents.plan_agent_contracts import PlanAgentRunResult
from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.money import CostItem
from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.validation import PlanValidationReport
from app.planning.material_contracts import PlanningMaterialBundle
from app.planning.repair_contracts import RepairRouterResult
from app.planning.revision_contracts import PlanRevisionResult
from app.planning.specialist_contracts import SpecialistFanoutResult
from app.planning.stateful_contracts import (
    HumanReviewAction,
    HumanReviewDecision,
    HumanReviewRequest,
    PlanningThreadStatus,
    StatefulPlanningNodeOutcome,
)
from app.planning.weather_indoor_recovery_contracts import WeatherIndoorRecoveryResult


class ProductPlanningNodeName(StrEnum):
    RUN_SPECIALISTS = "run_specialists"
    BUILD_MATERIALS = "build_materials"
    RUN_PLAN_AGENT = "run_plan_agent"
    VALIDATE_HARD_PLAN = "validate_hard_plan"
    RUN_REPAIR = "run_repair"
    PREPARE_HUMAN_REVIEW = "prepare_human_review"
    HUMAN_REVIEW = "human_review"
    APPLY_REVIEW_DECISION = "apply_review_decision"
    APPLY_PLAN_REVISION = "apply_plan_revision"


class ProductPlanningEvent(DomainModel):
    node: ProductPlanningNodeName
    outcome: StatefulPlanningNodeOutcome
    detail: NonEmptyText


class ProductPlanningProgress(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    node: ProductPlanningNodeName
    state_status: PlanningThreadStatus
    event: ProductPlanningEvent

    @model_validator(mode="after")
    def validate_node_lineage(self) -> "ProductPlanningProgress":
        if self.event.node != self.node:
            raise ValueError("progress node must match the committed product event")
        return self


class ProductPlanningData(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    workflow_version: Literal["product-planning-graph-v2"] = "product-planning-graph-v2"
    thread_id: Identifier
    request: TripRequest
    cost_items: tuple[CostItem, ...]
    data_mode: Literal[DataMode.LIVE, DataMode.FIXTURE]
    status: PlanningThreadStatus = PlanningThreadStatus.PLANNING
    specialists: SpecialistFanoutResult | None = None
    materials: PlanningMaterialBundle | None = None
    plan_agent: PlanAgentRunResult | None = None
    plan: TripPlan | None = None
    opening_hours: OpeningHoursEvidenceBundle | None = None
    validation: PlanValidationReport | None = None
    repair: RepairRouterResult | None = None
    review_request: HumanReviewRequest | None = None
    review_decision: HumanReviewDecision | None = None
    revision_result: PlanRevisionResult | None = None
    weather_indoor_recovery: WeatherIndoorRecoveryResult | None = None
    events: tuple[ProductPlanningEvent, ...] = ()

    @model_validator(mode="after")
    def validate_product_state(self) -> "ProductPlanningData":
        stage_nodes = (
            ProductPlanningNodeName.RUN_SPECIALISTS,
            ProductPlanningNodeName.BUILD_MATERIALS,
            ProductPlanningNodeName.RUN_PLAN_AGENT,
            ProductPlanningNodeName.VALIDATE_HARD_PLAN,
        )
        event_nodes = tuple(item.node for item in self.events)
        stage_payloads = (
            self.specialists,
            self.materials,
            self.plan_agent,
            self.validation,
        )
        completed_stages = sum(item is not None for item in stage_payloads)
        if any(item is None for item in stage_payloads[:completed_stages]):
            raise ValueError("product planning stages cannot contain gaps")
        if event_nodes[:completed_stages] != stage_nodes[:completed_stages]:
            raise ValueError("product planning events must preserve stage order")
        if self.specialists is not None and (
            self.specialists.request_id != self.request.request_id
            or self.specialists.data_mode != self.data_mode
        ):
            raise ValueError("specialist result must match the product request")
        if self.weather_indoor_recovery is not None and (
            self.weather_indoor_recovery.request_id != self.request.request_id
            or self.weather_indoor_recovery.data_mode != self.data_mode
        ):
            raise ValueError("weather indoor recovery must match the product request")
        if self.materials is not None and (
            self.materials.request_id != self.request.request_id
            or self.materials.data_mode != self.data_mode
        ):
            raise ValueError("planning materials must match the product request")
        if self.plan_agent is not None and self.plan_agent.request_id != self.request.request_id:
            raise ValueError("Plan Agent result must match the product request")
        if (self.plan is None) != (self.plan_agent is None):
            raise ValueError("product plan and Plan Agent result must appear together")
        if self.plan is not None and (
            self.plan_agent is None
            or self.plan_agent.plan is None
            or self.plan.request_id != self.request.request_id
            or self.plan.cost_items != self.cost_items
        ):
            raise ValueError("product plan must preserve Plan Agent lineage and supplied costs")
        if (self.opening_hours is None) != (self.validation is None):
            raise ValueError("hard validation requires an opening-hours evidence bundle")
        if self.opening_hours is not None and (
            self.opening_hours.request_id != self.request.request_id
            or self.opening_hours.data_mode != self.data_mode
        ):
            raise ValueError("opening-hours evidence must match the product request")
        if self.validation is not None and (
            self.plan is None
            or self.validation.request_id != self.request.request_id
            or self.validation.plan_id != self.plan.plan_id
            or self.validation.validator_version != "hard-trip-plan-validator-v1"
        ):
            raise ValueError("product graph must preserve the hard validation result")

        if self.status == PlanningThreadStatus.PLANNING:
            if completed_stages >= 4 or any(
                (
                    self.repair,
                    self.review_request,
                    self.review_decision,
                    self.revision_result,
                    self.weather_indoor_recovery,
                )
            ):
                raise ValueError("planning status may only contain incomplete product stages")
            return self

        if completed_stages != 4 or self.validation is None or self.plan is None:
            raise ValueError("post-planning state requires a hard-validated product plan")
        pipeline_prefix: tuple[ProductPlanningNodeName, ...] = stage_nodes
        if self.repair is not None:
            if (
                self.repair.request_id != self.request.request_id
                or self.repair.final_materials != self.materials
                or self.repair.final_plan != self.plan
                or self.repair.final_opening_hours != self.opening_hours
                or self.repair.final_report != self.validation
            ):
                raise ValueError("product repair result must preserve final planning artifacts")
            pipeline_prefix = (*stage_nodes, ProductPlanningNodeName.RUN_REPAIR)
        if event_nodes[: len(pipeline_prefix)] != pipeline_prefix:
            raise ValueError("product repair events must preserve planning lineage")
        if self.repair is None and ProductPlanningNodeName.RUN_REPAIR in event_nodes:
            raise ValueError("product repair events require a repair result")
        if self.status == PlanningThreadStatus.PLAN_READY:
            if any(
                (
                    self.review_request,
                    self.review_decision,
                    self.revision_result,
                    self.weather_indoor_recovery,
                )
            ):
                raise ValueError("plan_ready state cannot contain review data")
            return self

        effective_validation = (
            self.revision_result.validation
            if self.revision_result is not None
            else self.validation
        )
        if self.review_request is None:
            raise ValueError("review state requires a human review request")
        review_evidence_mismatch = self.review_request.request_id != self.request.request_id
        if self.status != PlanningThreadStatus.REVISION_APPLIED:
            review_evidence_mismatch = review_evidence_mismatch or (
                self.review_request.plan_id != effective_validation.plan_id
                or self.review_request.validation_status != effective_validation.status
                or self.review_request.can_finalize != effective_validation.can_finalize
                or self.review_request.issue_rule_codes
                != tuple(item.rule_code for item in effective_validation.issues)
            )
        if review_evidence_mismatch:
            raise ValueError("human review must preserve hard validation evidence")

        review_cycle = (
            ProductPlanningNodeName.PREPARE_HUMAN_REVIEW,
            ProductPlanningNodeName.HUMAN_REVIEW,
            ProductPlanningNodeName.APPLY_REVIEW_DECISION,
            ProductPlanningNodeName.APPLY_PLAN_REVISION,
        )
        status_tail = {
            PlanningThreadStatus.AWAITING_HUMAN_REVIEW: (
                ProductPlanningNodeName.PREPARE_HUMAN_REVIEW,
            ),
            PlanningThreadStatus.REVIEW_DECIDED: (
                ProductPlanningNodeName.PREPARE_HUMAN_REVIEW,
                ProductPlanningNodeName.HUMAN_REVIEW,
            ),
            PlanningThreadStatus.REVISION_REQUESTED: (
                ProductPlanningNodeName.PREPARE_HUMAN_REVIEW,
                ProductPlanningNodeName.HUMAN_REVIEW,
                ProductPlanningNodeName.APPLY_REVIEW_DECISION,
            ),
            PlanningThreadStatus.REVISION_APPLIED: review_cycle,
            PlanningThreadStatus.APPROVED_DRAFT: (
                ProductPlanningNodeName.PREPARE_HUMAN_REVIEW,
                ProductPlanningNodeName.HUMAN_REVIEW,
                ProductPlanningNodeName.APPLY_REVIEW_DECISION,
            ),
            PlanningThreadStatus.CONFLICT_ACKNOWLEDGED: (
                ProductPlanningNodeName.PREPARE_HUMAN_REVIEW,
                ProductPlanningNodeName.HUMAN_REVIEW,
                ProductPlanningNodeName.APPLY_REVIEW_DECISION,
            ),
            PlanningThreadStatus.CANCELLED: (
                ProductPlanningNodeName.PREPARE_HUMAN_REVIEW,
                ProductPlanningNodeName.HUMAN_REVIEW,
                ProductPlanningNodeName.APPLY_REVIEW_DECISION,
            ),
        }.get(self.status)
        if status_tail is None:
            raise ValueError("unsupported product review status")
        review_nodes = event_nodes[len(pipeline_prefix) :]
        if len(review_nodes) < len(status_tail) or review_nodes[-len(status_tail) :] != status_tail:
            raise ValueError("product review events must preserve the current review stage")
        completed_nodes = review_nodes[: len(review_nodes) - len(status_tail)]
        if len(completed_nodes) % len(review_cycle) != 0 or any(
            completed_nodes[index : index + len(review_cycle)] != review_cycle
            for index in range(0, len(completed_nodes), len(review_cycle))
        ):
            raise ValueError("product review events must preserve repeated revision cycles")

        if self.status == PlanningThreadStatus.AWAITING_HUMAN_REVIEW:
            if self.revision_result is None:
                if self.review_decision is not None:
                    raise ValueError("initial awaiting review cannot contain a decision")
            elif (
                self.review_decision is None
                or self.review_decision.action != HumanReviewAction.REQUEST_REVISION
                or self.review_decision.revision_request != self.revision_result.request
            ):
                raise ValueError("revised awaiting review must preserve the completed revision")
            return self
        if self.review_decision is None:
            raise ValueError("terminal product state requires a human decision")
        if (
            self.review_decision.review_id != self.review_request.review_id
            or self.review_decision.action not in self.review_request.allowed_actions
        ):
            raise ValueError("human decision must match the pending product review")
        if self.status == PlanningThreadStatus.REVIEW_DECIDED:
            return self
        if self.status == PlanningThreadStatus.REVISION_REQUESTED:
            if (
                self.review_decision.action != HumanReviewAction.REQUEST_REVISION
                or self.review_decision.revision_request is None
            ):
                raise ValueError("revision_requested must preserve its structured decision")
            return self
        if self.status == PlanningThreadStatus.REVISION_APPLIED:
            if (
                self.revision_result is None
                or self.review_decision.action != HumanReviewAction.REQUEST_REVISION
                or self.review_decision.revision_request != self.revision_result.request
                or self.revision_result.diff.from_plan_id
                != self.revision_result.request.base_plan_id
                or self.revision_result.validation.validator_version
                != "hard-trip-plan-validator-v1"
            ):
                raise ValueError("revision_applied must preserve hard-validated revision lineage")
            return self
        expected_status = {
            "approve_draft": PlanningThreadStatus.APPROVED_DRAFT,
            "acknowledge_conflict": PlanningThreadStatus.CONFLICT_ACKNOWLEDGED,
            "cancel": PlanningThreadStatus.CANCELLED,
        }.get(self.review_decision.action.value)
        if expected_status is None or self.status != expected_status:
            raise ValueError("terminal product status must follow the human decision")
        return self


class ProductPlanningSnapshot(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    thread_id: Identifier
    checkpoint_id: NonEmptyText
    next_nodes: tuple[NonEmptyText, ...]
    state: ProductPlanningData

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ProductPlanningSnapshot":
        if self.thread_id != self.state.thread_id:
            raise ValueError("snapshot thread_id must match the persisted product state")
        awaiting = self.state.status == PlanningThreadStatus.AWAITING_HUMAN_REVIEW
        if awaiting != (self.next_nodes == (ProductPlanningNodeName.HUMAN_REVIEW.value,)):
            raise ValueError("snapshot next_nodes must reflect the product review interrupt")
        return self
