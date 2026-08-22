from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.money import CostItem
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.planning.stateful_contracts import (
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
    node: StatefulPlanningNodeName | None = None
    state_status: PlanningThreadStatus | None = None
    review_id: Identifier | None = None
    error_code: Identifier | None = None

    @model_validator(mode="after")
    def validate_event_shape(self) -> "PlanningTaskEvent":
        is_node_event = self.kind == PlanningTaskEventKind.GRAPH_NODE_COMPLETED
        if is_node_event != (self.node is not None and self.state_status is not None):
            raise ValueError("graph node events must contain node and state_status only together")
        if not is_node_event and (self.node is not None or self.state_status is not None):
            raise ValueError("non-node events cannot contain graph node fields")
        if (self.kind == PlanningTaskEventKind.TASK_AWAITING_INPUT) != (self.review_id is not None):
            raise ValueError("awaiting-input events must contain review_id")
        if (self.kind == PlanningTaskEventKind.TASK_FAILED) != (self.error_code is not None):
            raise ValueError("failed events must contain error_code")
        return self


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
    result: StatefulPlanningSnapshot | None = None
    failure: PlanningTaskFailure | None = None

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
        return self


class PlanningTaskAccepted(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: Identifier
    request_id: Identifier
    status: Literal[PlanningTaskStatus.QUEUED] = PlanningTaskStatus.QUEUED
    task_url: NonEmptyText
    events_url: NonEmptyText


def utc_now() -> datetime:
    return datetime.now(UTC)
