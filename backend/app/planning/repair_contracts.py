from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import TripPlan
from app.domain.validation import PlanValidationReport, RepairAction, ResponsibleNode
from app.planning.material_contracts import PlanningMaterialBundle


class RepairExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RepairOutcome(StrEnum):
    ALREADY_FINALIZABLE = "already_finalizable"
    REPAIRED = "repaired"
    WAITING_FOR_USER = "waiting_for_user"
    UNRESOLVED = "unresolved"


class RepairStopReason(StrEnum):
    FINALIZABLE = "finalizable"
    USER_CONFIRMATION_REQUIRED = "user_confirmation_required"
    UNREPAIRABLE_ISSUE = "unrepairable_issue"
    RETRY_LIMIT_REACHED = "retry_limit_reached"


class RepairArtifactHashes(DomainModel):
    materials_sha256: Sha256Digest
    plan_sha256: Sha256Digest
    opening_hours_sha256: Sha256Digest


class RepairPlanDiff(DomainModel):
    changed_dates: tuple[date, ...] = ()
    added_candidate_ids: tuple[Identifier, ...] = ()
    removed_candidate_ids: tuple[Identifier, ...] = ()
    total_cost_minimum_before: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    total_cost_minimum_after: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    total_cost_maximum_before: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    total_cost_maximum_after: Decimal = Field(ge=0, max_digits=12, decimal_places=2)

    @model_validator(mode="after")
    def validate_diff(self) -> "RepairPlanDiff":
        for values in (
            self.changed_dates,
            self.added_candidate_ids,
            self.removed_candidate_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("repair plan diff values must be unique")
        if set(self.added_candidate_ids) & set(self.removed_candidate_ids):
            raise ValueError("a candidate cannot be both added and removed")
        return self


class RepairExecutionResult(DomainModel):
    status: RepairExecutionStatus
    materials: PlanningMaterialBundle
    plan: TripPlan
    opening_hours: OpeningHoursEvidenceBundle
    executed_nodes: tuple[ResponsibleNode, ...] = ()
    model_call_count: int = Field(default=0, ge=0)
    provider_call_count: int = Field(default=0, ge=0)
    error_code: Identifier | None = None

    @model_validator(mode="after")
    def validate_execution(self) -> "RepairExecutionResult":
        if len(self.executed_nodes) != len(set(self.executed_nodes)):
            raise ValueError("repair execution nodes must be unique")
        if (self.status == RepairExecutionStatus.FAILED) != (self.error_code is not None):
            raise ValueError("failed repair execution requires exactly one error code")
        return self


class RepairAttemptTrace(DomainModel):
    attempt_index: int = Field(ge=1)
    action_attempt: int = Field(ge=1, le=2)
    repair_action: RepairAction
    responsible_node: ResponsibleNode
    trigger_issue_codes: tuple[NonEmptyText, ...] = Field(min_length=1)
    execution_status: RepairExecutionStatus
    executed_nodes: tuple[ResponsibleNode, ...]
    reused_nodes: tuple[ResponsibleNode, ...]
    before_error_codes: tuple[NonEmptyText, ...]
    after_error_codes: tuple[NonEmptyText, ...]
    resolved_issue_codes: tuple[NonEmptyText, ...]
    introduced_issue_codes: tuple[NonEmptyText, ...]
    before_hashes: RepairArtifactHashes
    after_hashes: RepairArtifactHashes
    plan_diff: RepairPlanDiff
    model_call_count: int = Field(ge=0)
    provider_call_count: int = Field(ge=0)
    error_code: Identifier | None = None

    @model_validator(mode="after")
    def validate_trace(self) -> "RepairAttemptTrace":
        unique_collections = (
            self.trigger_issue_codes,
            self.executed_nodes,
            self.reused_nodes,
            self.before_error_codes,
            self.after_error_codes,
            self.resolved_issue_codes,
            self.introduced_issue_codes,
        )
        if any(len(values) != len(set(values)) for values in unique_collections):
            raise ValueError("repair attempt trace collections must contain unique values")
        if set(self.executed_nodes) & set(self.reused_nodes):
            raise ValueError("executed and reused repair nodes cannot overlap")
        if set(self.resolved_issue_codes) != (
            set(self.before_error_codes) - set(self.after_error_codes)
        ):
            raise ValueError("resolved issue codes must equal the before/after error diff")
        if set(self.introduced_issue_codes) != (
            set(self.after_error_codes) - set(self.before_error_codes)
        ):
            raise ValueError("introduced issue codes must equal the after/before error diff")
        if (self.execution_status == RepairExecutionStatus.FAILED) != (self.error_code is not None):
            raise ValueError("failed repair trace requires exactly one error code")
        return self


class RepairRetryCount(DomainModel):
    repair_action: RepairAction
    attempt_count: int = Field(ge=1, le=2)


class RepairRouterResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    router_version: Literal["repair-router-v1"] = "repair-router-v1"
    request_id: Identifier
    outcome: RepairOutcome
    stop_reason: RepairStopReason
    initial_report: PlanValidationReport
    final_report: PlanValidationReport
    final_materials: PlanningMaterialBundle
    final_plan: TripPlan
    final_opening_hours: OpeningHoursEvidenceBundle
    attempts: tuple[RepairAttemptTrace, ...] = Field(max_length=12)
    retry_counts: tuple[RepairRetryCount, ...] = Field(max_length=6)
    pending_error_codes: tuple[NonEmptyText, ...]
    requires_user_confirmation: bool
    total_model_call_count: int = Field(ge=0)
    total_provider_call_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_result(self) -> "RepairRouterResult":
        identities = {
            self.request_id,
            self.initial_report.request_id,
            self.final_report.request_id,
            self.final_materials.request_id,
            self.final_plan.request_id,
            self.final_opening_hours.request_id,
        }
        if len(identities) != 1:
            raise ValueError("repair result artifacts must preserve request identity")
        if self.final_report.plan_id != self.final_plan.plan_id:
            raise ValueError("repair final report must describe the final plan")
        if tuple(item.attempt_index for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("repair attempt indexes must be contiguous")
        action_counts: dict[RepairAction, int] = {}
        for attempt in self.attempts:
            action_counts[attempt.repair_action] = action_counts.get(attempt.repair_action, 0) + 1
            if attempt.action_attempt != action_counts[attempt.repair_action]:
                raise ValueError("repair action attempt indexes must be contiguous")
        expected_retry_counts = tuple(
            RepairRetryCount(repair_action=action, attempt_count=count)
            for action, count in action_counts.items()
        )
        if self.retry_counts != expected_retry_counts:
            raise ValueError("repair retry counts must match attempt traces")
        if self.total_model_call_count != sum(item.model_call_count for item in self.attempts):
            raise ValueError("repair model-call total must match attempt traces")
        if self.total_provider_call_count != sum(
            item.provider_call_count for item in self.attempts
        ):
            raise ValueError("repair provider-call total must match attempt traces")
        expected_pending = tuple(
            item.rule_code for item in self.final_report.issues if item.severity.value == "error"
        )
        if self.pending_error_codes != expected_pending:
            raise ValueError("pending repair errors must match the final validation report")
        finalizable = self.final_report.can_finalize
        if finalizable != (self.stop_reason == RepairStopReason.FINALIZABLE):
            raise ValueError("repair stop reason must match finalization state")
        if self.outcome == RepairOutcome.ALREADY_FINALIZABLE and self.attempts:
            raise ValueError("already-finalizable repair results cannot contain attempts")
        if self.outcome == RepairOutcome.REPAIRED and not self.attempts:
            raise ValueError("repaired outcomes require at least one attempt")
        if self.stop_reason == RepairStopReason.FINALIZABLE and self.outcome not in {
            RepairOutcome.ALREADY_FINALIZABLE,
            RepairOutcome.REPAIRED,
        }:
            raise ValueError("finalizable repair results require a successful outcome")
        if self.outcome == RepairOutcome.UNRESOLVED and self.stop_reason not in {
            RepairStopReason.UNREPAIRABLE_ISSUE,
            RepairStopReason.RETRY_LIMIT_REACHED,
        }:
            raise ValueError("unresolved repair results require an unresolved stop reason")
        if self.outcome == RepairOutcome.WAITING_FOR_USER:
            if (
                self.stop_reason != RepairStopReason.USER_CONFIRMATION_REQUIRED
                or not self.requires_user_confirmation
            ):
                raise ValueError("waiting-for-user outcomes require a confirmation stop")
        elif self.requires_user_confirmation:
            raise ValueError("only waiting-for-user outcomes may require confirmation")
        return self
