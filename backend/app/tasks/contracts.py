from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.money import CostItem
from app.domain.planning import PlanVersion
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.planning.product_contracts import (
    ProductPlanningNodeName,
    ProductPlanningSnapshot,
)
from app.planning.revision_contracts import PlanRevisionRequest
from app.planning.stateful_contracts import (
    HumanReviewAction,
    PlanningThreadStatus,
    StatefulPlanningNodeName,
    StatefulPlanningSnapshot,
)

PLANNING_TASK_WORKFLOW_VERSION: Literal["planning-task-api-v1"] = "planning-task-api-v1"


class PlanningTaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PlanningTaskEventKind(StrEnum):
    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    GRAPH_NODE_COMPLETED = "graph_node_completed"
    TASK_AWAITING_INPUT = "task_awaiting_input"
    TASK_REVIEW_SUBMITTED = "task_review_submitted"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"


class PlanningTaskFailureCategory(StrEnum):
    PROVIDER = "provider"
    CONFIGURATION = "configuration"
    WORKFLOW = "workflow"
    INTERNAL = "internal"


class PlanningTaskCreateRequest(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    request: TripRequest
    selected_destination_adcode: str | None = Field(default=None, pattern=r"^\d{6}$")
    cost_items: tuple[CostItem, ...] = ()
    data_mode: Literal[DataMode.FIXTURE, DataMode.LIVE] = DataMode.FIXTURE


class PlanningTaskSubmission(PlanningTaskCreateRequest):
    task_id: Identifier


class PlanningTaskFailure(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    error_code: Identifier
    category: PlanningTaskFailureCategory
    retryable: bool
    user_message: NonEmptyText


class PlanningTaskEvent(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    workflow_version: Literal["planning-task-api-v1"] = PLANNING_TASK_WORKFLOW_VERSION
    event_id: Identifier
    sequence: int = Field(ge=1)
    task_id: Identifier
    kind: PlanningTaskEventKind
    task_status: PlanningTaskStatus
    occurred_at: AwareDatetime
    message: NonEmptyText
    node: StatefulPlanningNodeName | ProductPlanningNodeName | None = None
    state_status: PlanningThreadStatus | None = None
    review_id: Identifier | None = None
    review_action: HumanReviewAction | None = None
    error_code: Identifier | None = None

    @model_validator(mode="after")
    def validate_event_shape(self) -> "PlanningTaskEvent":
        is_node_event = self.kind == PlanningTaskEventKind.GRAPH_NODE_COMPLETED
        if is_node_event != (self.node is not None and self.state_status is not None):
            raise ValueError("graph node events must contain node and state_status only together")
        if not is_node_event and (self.node is not None or self.state_status is not None):
            raise ValueError("non-node events cannot contain graph node fields")
        is_review_event = self.kind in {
            PlanningTaskEventKind.TASK_AWAITING_INPUT,
            PlanningTaskEventKind.TASK_REVIEW_SUBMITTED,
        }
        if is_review_event != (self.review_id is not None):
            raise ValueError("review events must contain review_id")
        is_review_submission = self.kind == PlanningTaskEventKind.TASK_REVIEW_SUBMITTED
        if is_review_submission != (self.review_action is not None):
            raise ValueError("review-submitted events must contain review_action")
        if (self.kind == PlanningTaskEventKind.TASK_FAILED) != (self.error_code is not None):
            raise ValueError("failed events must contain error_code")
        return self


class PlanningTaskPlanDiff(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    from_version_id: Identifier
    to_version_id: Identifier
    plan_changed: bool
    changed_dates: tuple[date, ...] = ()
    added_item_ids: tuple[Identifier, ...] = ()
    removed_item_ids: tuple[Identifier, ...] = ()
    rescheduled_item_ids: tuple[Identifier, ...] = ()
    summary: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_diff(self) -> "PlanningTaskPlanDiff":
        structural_changes = (
            self.changed_dates,
            self.added_item_ids,
            self.removed_item_ids,
            self.rescheduled_item_ids,
        )
        if self.plan_changed != any(structural_changes):
            raise ValueError("plan_changed must match structural diff fields")
        if not self.plan_changed and self.from_version_id != self.to_version_id:
            raise ValueError("unchanged plans must keep the same version")
        return self


class PlanningTaskReviewOutcome(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: Identifier
    review_id: Identifier
    action: HumanReviewAction
    reviewer_id: Identifier
    comment: str | None = None
    decided_at: AwareDatetime
    resulting_state_status: PlanningThreadStatus
    plan_diff: PlanningTaskPlanDiff


class PlanningTaskSnapshot(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    workflow_version: Literal["planning-task-api-v1"] = PLANNING_TASK_WORKFLOW_VERSION
    task_id: Identifier
    request_id: Identifier
    data_mode: Literal[DataMode.FIXTURE, DataMode.LIVE]
    status: PlanningTaskStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    event_count: int = Field(ge=1)
    result: StatefulPlanningSnapshot | ProductPlanningSnapshot | None = None
    failure: PlanningTaskFailure | None = None
    plan_versions: tuple[PlanVersion, ...] = ()
    review_outcome: PlanningTaskReviewOutcome | None = None

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> "PlanningTaskSnapshot":
        if self.updated_at < self.created_at:
            raise ValueError("task updated_at cannot precede created_at")
        has_result = self.status in {
            PlanningTaskStatus.AWAITING_INPUT,
            PlanningTaskStatus.SUCCEEDED,
        }
        if has_result != (self.result is not None):
            raise ValueError("awaiting-input and succeeded tasks must contain a result")
        if (self.status == PlanningTaskStatus.FAILED) != (self.failure is not None):
            raise ValueError("failed tasks must contain failure details")
        if self.result is not None and self.result.thread_id != self.task_id:
            raise ValueError("task result thread_id must match task_id")
        version_numbers = tuple(item.version_number for item in self.plan_versions)
        if version_numbers != tuple(range(1, len(self.plan_versions) + 1)):
            raise ValueError("plan versions must be contiguous and ordered")
        if any(
            current.based_on_version_id != previous.version_id
            for previous, current in zip(self.plan_versions, self.plan_versions[1:], strict=False)
        ):
            raise ValueError("plan versions must preserve direct parent lineage")
        if self.result is not None and not self.plan_versions:
            raise ValueError("task results require at least one plan version")
        if (
            self.result is not None
            and self.result.state.revision_result is not None
            and self.plan_versions[-1].plan != self.result.state.revision_result.revised_plan
        ):
            raise ValueError("latest plan version must preserve the revision result")
        if self.review_outcome is not None:
            if self.status != PlanningTaskStatus.SUCCEEDED or self.result is None:
                raise ValueError("review outcome requires a succeeded task result")
            if self.result.state.review_decision is None:
                raise ValueError("review outcome requires a persisted graph decision")
            known_versions = {item.version_id for item in self.plan_versions}
            if {
                self.review_outcome.plan_diff.from_version_id,
                self.review_outcome.plan_diff.to_version_id,
            } - known_versions:
                raise ValueError("review outcome must reference known plan versions")
        return self


class PlanningTaskAccepted(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: Identifier
    request_id: Identifier
    status: Literal[PlanningTaskStatus.QUEUED] = PlanningTaskStatus.QUEUED
    task_url: NonEmptyText
    events_url: NonEmptyText


class PlanningTaskReviewDecisionRequest(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: Identifier
    review_id: Identifier
    action: HumanReviewAction
    reviewer_id: Identifier
    comment: str | None = Field(default=None, min_length=1, max_length=500)
    revision_request: PlanRevisionRequest | None = None

    @model_validator(mode="after")
    def validate_revision_comment(self) -> "PlanningTaskReviewDecisionRequest":
        is_revision = self.action == HumanReviewAction.REQUEST_REVISION
        if is_revision != (self.revision_request is not None):
            raise ValueError("request_revision requires exactly one structured revision request")
        if is_revision and self.comment is None:
            raise ValueError("request_revision requires a comment")
        return self


class PlanningTaskReviewDecisionAccepted(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: Identifier
    task_id: Identifier
    review_id: Identifier
    action: HumanReviewAction
    status: Literal[PlanningTaskStatus.RUNNING] = PlanningTaskStatus.RUNNING
    idempotent_replay: bool
    task_url: NonEmptyText
    events_url: NonEmptyText


def utc_now() -> datetime:
    return datetime.now(UTC)
