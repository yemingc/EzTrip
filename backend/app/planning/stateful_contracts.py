from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.money import CostItem
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.validation import PlanValidationStatus
from app.planning.revision_contracts import PlanRevisionRequest, PlanRevisionResult
from app.planning.vertical_slice import VerticalSliceResult


class HumanReviewKind(StrEnum):
    PLAN_APPROVAL = "plan_approval"
    CONFLICT_RESOLUTION = "conflict_resolution"


class HumanReviewAction(StrEnum):
    APPROVE_DRAFT = "approve_draft"
    ACKNOWLEDGE_CONFLICT = "acknowledge_conflict"
    REQUEST_REVISION = "request_revision"
    CANCEL = "cancel"


class PlanningThreadStatus(StrEnum):
    PLANNING = "planning"
    PLAN_READY = "plan_ready"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    REVIEW_DECIDED = "review_decided"
    APPROVED_DRAFT = "approved_draft"
    CONFLICT_ACKNOWLEDGED = "conflict_acknowledged"
    REVISION_REQUESTED = "revision_requested"
    REVISION_APPLIED = "revision_applied"
    CANCELLED = "cancelled"


class StatefulPlanningNodeName(StrEnum):
    RUN_VERTICAL_SLICE = "run_vertical_slice"
    PREPARE_HUMAN_REVIEW = "prepare_human_review"
    HUMAN_REVIEW = "human_review"
    APPLY_REVIEW_DECISION = "apply_review_decision"
    APPLY_PLAN_REVISION = "apply_plan_revision"


class StatefulPlanningNodeOutcome(StrEnum):
    PLANNED = "planned"
    REVIEW_REQUIRED = "review_required"
    RESUMED = "resumed"
    REVISED = "revised"
    COMPLETED = "completed"


class StatefulPlanningEvent(DomainModel):
    node: StatefulPlanningNodeName
    outcome: StatefulPlanningNodeOutcome
    detail: NonEmptyText


class StatefulPlanningProgress(DomainModel):
    """One committed graph-node update, derived from LangGraph's update stream."""

    schema_version: Literal["1.0"] = "1.0"
    node: StatefulPlanningNodeName
    state_status: PlanningThreadStatus
    event: StatefulPlanningEvent

    @model_validator(mode="after")
    def validate_node_lineage(self) -> "StatefulPlanningProgress":
        if self.event.node != self.node:
            raise ValueError("progress node must match the committed planning event")
        return self


class HumanReviewRequest(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    review_id: Identifier
    kind: HumanReviewKind
    request_id: Identifier
    plan_id: Identifier
    prompt: NonEmptyText
    allowed_actions: tuple[HumanReviewAction, ...] = Field(min_length=1)
    validation_status: PlanValidationStatus
    can_finalize: bool
    issue_rule_codes: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_review_policy(self) -> "HumanReviewRequest":
        if len(self.allowed_actions) != len(set(self.allowed_actions)):
            raise ValueError("human review actions must be unique")
        if len(self.issue_rule_codes) != len(set(self.issue_rule_codes)):
            raise ValueError("human review issue rule codes must be unique")
        expected_actions = (
            HumanReviewAction.APPROVE_DRAFT,
            HumanReviewAction.REQUEST_REVISION,
            HumanReviewAction.CANCEL,
        )
        if self.kind == HumanReviewKind.CONFLICT_RESOLUTION:
            expected_actions = (
                HumanReviewAction.ACKNOWLEDGE_CONFLICT,
                HumanReviewAction.REQUEST_REVISION,
                HumanReviewAction.CANCEL,
            )
            if self.can_finalize or self.validation_status != PlanValidationStatus.CONFLICTED:
                raise ValueError("conflict review requires a non-finalizable conflicted plan")
        elif not self.can_finalize or self.validation_status == PlanValidationStatus.CONFLICTED:
            raise ValueError("plan approval requires a finalizable non-conflicted draft")
        if self.allowed_actions != expected_actions:
            raise ValueError("allowed actions must match the deterministic review policy")
        return self


class HumanReviewResume(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    review_id: Identifier
    action: HumanReviewAction
    reviewer_id: Identifier
    comment: NonEmptyText | None = None
    revision_request: PlanRevisionRequest | None = None

    @model_validator(mode="after")
    def validate_revision_payload(self) -> "HumanReviewResume":
        is_revision = self.action == HumanReviewAction.REQUEST_REVISION
        if is_revision != (self.revision_request is not None):
            raise ValueError("request_revision requires exactly one structured revision request")
        if is_revision and self.comment is None:
            raise ValueError("request_revision requires a reviewer comment")
        return self


class HumanReviewDecision(HumanReviewResume):
    decided_at: AwareDatetime


class StatefulPlanningData(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    workflow_version: Literal["stateful-planning-checkpoint-v1"] = "stateful-planning-checkpoint-v1"
    thread_id: Identifier
    request: TripRequest
    cost_items: tuple[CostItem, ...]
    data_mode: Literal[DataMode.LIVE, DataMode.FIXTURE]
    status: PlanningThreadStatus = PlanningThreadStatus.PLANNING
    vertical_slice: VerticalSliceResult | None = None
    review_request: HumanReviewRequest | None = None
    review_decision: HumanReviewDecision | None = None
    revision_result: PlanRevisionResult | None = None
    events: tuple[StatefulPlanningEvent, ...] = ()

    @model_validator(mode="after")
    def validate_state_machine(self) -> "StatefulPlanningData":
        if self.status == PlanningThreadStatus.PLANNING:
            if any(
                (
                    self.vertical_slice,
                    self.review_request,
                    self.review_decision,
                    self.revision_result,
                    self.events,
                )
            ):
                raise ValueError("planning state cannot contain downstream results")
            return self

        if self.vertical_slice is None:
            raise ValueError("post-planning state requires a vertical slice result")
        if self.vertical_slice.request_id != self.request.request_id:
            raise ValueError("state request must match the vertical slice request")
        if self.vertical_slice.data_mode != self.data_mode:
            raise ValueError("state data_mode must match the vertical slice")
        if self.vertical_slice.plan.cost_items != self.cost_items:
            raise ValueError("state cost_items must match the assembled plan")

        if self.status == PlanningThreadStatus.PLAN_READY:
            if (
                self.review_request is not None
                or self.review_decision is not None
                or self.revision_result is not None
            ):
                raise ValueError("plan_ready state cannot contain review data")
            if tuple(event.node for event in self.events) != (
                StatefulPlanningNodeName.RUN_VERTICAL_SLICE,
            ):
                raise ValueError("plan_ready state must follow the planning node")
            return self

        if self.review_request is None:
            raise ValueError("review state requires a human review request")

        review = self.review_request
        validation = self.vertical_slice.validation
        if review.request_id != self.request.request_id or review.plan_id != validation.plan_id:
            raise ValueError("human review must reference the current request and plan")
        if (
            review.validation_status != validation.status
            or review.can_finalize != validation.can_finalize
            or review.issue_rule_codes != tuple(item.rule_code for item in validation.issues)
        ):
            raise ValueError("human review must preserve the validation result")

        expected_prefix = (
            StatefulPlanningNodeName.RUN_VERTICAL_SLICE,
            StatefulPlanningNodeName.PREPARE_HUMAN_REVIEW,
        )
        event_nodes = tuple(event.node for event in self.events)
        if event_nodes[:2] != expected_prefix:
            raise ValueError("stateful planning events must preserve node order")

        if self.status == PlanningThreadStatus.AWAITING_HUMAN_REVIEW:
            if (
                self.review_decision is not None
                or self.revision_result is not None
                or event_nodes != expected_prefix
            ):
                raise ValueError("awaiting review state cannot contain a decision")
            return self

        if self.review_decision is None:
            raise ValueError("terminal planning state requires a human decision")
        decision = self.review_decision
        if decision.review_id != review.review_id:
            raise ValueError("human decision must reference the pending review")
        if decision.action not in review.allowed_actions:
            raise ValueError("human decision action is not allowed for this review")
        if self.status == PlanningThreadStatus.REVIEW_DECIDED:
            expected_decided_nodes = (
                *expected_prefix,
                StatefulPlanningNodeName.HUMAN_REVIEW,
            )
            if self.revision_result is not None or event_nodes != expected_decided_nodes:
                raise ValueError("review_decided state must follow the review node")
            return self
        decision_nodes = (
            *expected_prefix,
            StatefulPlanningNodeName.HUMAN_REVIEW,
            StatefulPlanningNodeName.APPLY_REVIEW_DECISION,
        )
        if self.status == PlanningThreadStatus.REVISION_REQUESTED:
            if (
                decision.action != HumanReviewAction.REQUEST_REVISION
                or decision.revision_request is None
                or self.revision_result is not None
                or event_nodes != decision_nodes
            ):
                raise ValueError("revision_requested state must preserve its structured decision")
            return self
        if self.status == PlanningThreadStatus.REVISION_APPLIED:
            expected_revision_nodes = (
                *decision_nodes,
                StatefulPlanningNodeName.APPLY_PLAN_REVISION,
            )
            if (
                decision.action != HumanReviewAction.REQUEST_REVISION
                or decision.revision_request is None
                or self.revision_result is None
                or event_nodes != expected_revision_nodes
            ):
                raise ValueError("revision_applied state must follow the revision node")
            if (
                self.revision_result.request != decision.revision_request
                or self.revision_result.revised_plan.request_id != self.request.request_id
                or self.revision_result.diff.from_plan_id != self.vertical_slice.plan.plan_id
            ):
                raise ValueError("revision result must preserve decision and plan lineage")
            return self
        if self.revision_result is not None or event_nodes != decision_nodes:
            raise ValueError("terminal planning events must preserve node order")
        expected_status = {
            HumanReviewAction.APPROVE_DRAFT: PlanningThreadStatus.APPROVED_DRAFT,
            HumanReviewAction.ACKNOWLEDGE_CONFLICT: (PlanningThreadStatus.CONFLICT_ACKNOWLEDGED),
            HumanReviewAction.CANCEL: PlanningThreadStatus.CANCELLED,
        }.get(decision.action)
        if expected_status is None:
            raise ValueError("request_revision must continue to the revision node")
        if self.status != expected_status:
            raise ValueError("terminal status must follow the human decision")
        return self


class StatefulPlanningSnapshot(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    thread_id: Identifier
    checkpoint_id: NonEmptyText
    next_nodes: tuple[NonEmptyText, ...]
    state: StatefulPlanningData

    @model_validator(mode="after")
    def validate_snapshot(self) -> "StatefulPlanningSnapshot":
        if self.thread_id != self.state.thread_id:
            raise ValueError("snapshot thread_id must match the persisted state")
        awaiting = self.state.status == PlanningThreadStatus.AWAITING_HUMAN_REVIEW
        if awaiting != (self.next_nodes == (StatefulPlanningNodeName.HUMAN_REVIEW.value,)):
            raise ValueError("snapshot next_nodes must reflect the review interrupt")
        return self


class CheckpointHistoryEntry(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    step: int = Field(ge=-1)
    source: NonEmptyText
    write_nodes: tuple[NonEmptyText, ...]
    next_nodes: tuple[NonEmptyText, ...]
    state_status: PlanningThreadStatus | None = None


Clock = Callable[[], datetime]
ProgressCallback = Callable[[StatefulPlanningProgress], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(UTC)
