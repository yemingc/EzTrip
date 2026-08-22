from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.candidates import ActivityEnvironment
from app.domain.planning import TripPlan
from app.domain.sources import SourceReference
from app.domain.travel_data import RiskSeverity
from app.domain.validation import PlanValidationReport
from app.planning.repair_contracts import RepairPlanDiff


class WeatherRepairOutcome(StrEnum):
    NO_ACTION = "no_action"
    AUTO_APPLIED = "auto_applied"
    WAITING_FOR_USER = "waiting_for_user"
    UNRESOLVED = "unresolved"


class WeatherRepairStopReason(StrEnum):
    NO_SIGNIFICANT_IMPACT = "no_significant_impact"
    FINALIZABLE = "finalizable"
    USER_CONFIRMATION_REQUIRED = "user_confirmation_required"
    RETRY_LIMIT_REACHED = "retry_limit_reached"


class WeatherChangeGrade(StrEnum):
    NONE = "none"
    MINOR = "minor"
    MAJOR = "major"


class WeatherReplanExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WeatherImpact(DomainModel):
    risk_id: Identifier
    item_id: Identifier
    candidate_id: Identifier
    service_date: date
    environment: ActivityEnvironment
    severity: RiskSeverity
    matched_activity_types: tuple[NonEmptyText, ...] = Field(min_length=1)
    risk_source: SourceReference


class WeatherRepairTask(DomainModel):
    task_id: Identifier
    request_id: Identifier
    trigger: Literal["weather_provider_risk"] = "weather_provider_risk"
    affected_dates: tuple[date, ...] = Field(min_length=1)
    protected_dates: tuple[date, ...]
    risk_ids: tuple[Identifier, ...] = Field(min_length=1)
    impacted_item_ids: tuple[Identifier, ...] = Field(min_length=1)
    max_attempts: int = Field(default=2, ge=1, le=2)

    @model_validator(mode="after")
    def validate_task_scope(self) -> "WeatherRepairTask":
        collections = (
            self.affected_dates,
            self.protected_dates,
            self.risk_ids,
            self.impacted_item_ids,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("weather repair task collections must contain unique values")
        if set(self.affected_dates) & set(self.protected_dates):
            raise ValueError("affected and protected weather repair dates cannot overlap")
        return self


class WeatherReplanExecutionResult(DomainModel):
    status: WeatherReplanExecutionStatus
    proposed_plan: TripPlan | None = None
    model_call_count: int = Field(default=0, ge=0, le=1)
    provider_call_count: int = Field(default=0, ge=0)
    error_code: Identifier | None = None

    @model_validator(mode="after")
    def validate_execution(self) -> "WeatherReplanExecutionResult":
        failed = self.status == WeatherReplanExecutionStatus.FAILED
        if failed != (self.error_code is not None):
            raise ValueError("failed weather replans require exactly one error code")
        if (failed and self.proposed_plan is not None) or (
            not failed and self.proposed_plan is None
        ):
            raise ValueError("only successful weather replans carry a proposed plan")
        return self


class WeatherPlanChange(DomainModel):
    grade: WeatherChangeGrade
    diff: RepairPlanDiff
    changed_item_ids: tuple[Identifier, ...]
    cross_day_candidate_ids: tuple[Identifier, ...]
    major_reasons: tuple[NonEmptyText, ...]

    @model_validator(mode="after")
    def validate_grade(self) -> "WeatherPlanChange":
        for values in (
            self.changed_item_ids,
            self.cross_day_candidate_ids,
            self.major_reasons,
        ):
            if len(values) != len(set(values)):
                raise ValueError("weather change collections must contain unique values")
        if self.grade == WeatherChangeGrade.MAJOR and not self.major_reasons:
            raise ValueError("major weather changes require reasons")
        if self.grade != WeatherChangeGrade.MAJOR and self.major_reasons:
            raise ValueError("only major weather changes carry major reasons")
        if self.grade == WeatherChangeGrade.NONE and self.diff.changed_dates:
            raise ValueError("unchanged weather proposals cannot contain changed dates")
        return self


class WeatherRepairAttemptTrace(DomainModel):
    attempt_index: int = Field(ge=1, le=2)
    execution_status: WeatherReplanExecutionStatus
    scope_valid: bool
    remaining_impact_count: int = Field(ge=0)
    change: WeatherPlanChange
    validation_report: PlanValidationReport | None = None
    model_call_count: int = Field(ge=0, le=1)
    provider_call_count: int = Field(ge=0)
    error_code: Identifier | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> "WeatherRepairAttemptTrace":
        if (self.execution_status == WeatherReplanExecutionStatus.FAILED) != (
            self.error_code is not None
        ):
            raise ValueError("failed weather attempt traces require exactly one error code")
        return self


class WeatherRepairResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    coordinator_version: Literal["weather-repair-v1"] = "weather-repair-v1"
    request_id: Identifier
    outcome: WeatherRepairOutcome
    stop_reason: WeatherRepairStopReason
    impacts: tuple[WeatherImpact, ...]
    task: WeatherRepairTask | None = None
    initial_plan: TripPlan
    effective_plan: TripPlan
    proposed_plan: TripPlan | None = None
    change: WeatherPlanChange
    validation_report: PlanValidationReport | None = None
    attempts: tuple[WeatherRepairAttemptTrace, ...] = Field(max_length=2)
    requires_user_confirmation: bool
    total_model_call_count: int = Field(ge=0, le=2)
    total_provider_call_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_result(self) -> "WeatherRepairResult":
        if {self.request_id, self.initial_plan.request_id, self.effective_plan.request_id} != {
            self.request_id
        }:
            raise ValueError("weather repair plans must preserve request identity")
        if self.proposed_plan is not None and self.proposed_plan.request_id != self.request_id:
            raise ValueError("weather repair proposal must preserve request identity")
        if tuple(item.attempt_index for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("weather repair attempt indexes must be contiguous")
        if self.total_model_call_count != sum(item.model_call_count for item in self.attempts):
            raise ValueError("weather repair model-call total must match attempts")
        if self.total_provider_call_count != sum(
            item.provider_call_count for item in self.attempts
        ):
            raise ValueError("weather repair provider-call total must match attempts")
        if self.outcome == WeatherRepairOutcome.NO_ACTION:
            if self.impacts or self.task or self.attempts or self.proposed_plan is not None:
                raise ValueError("no-action weather results cannot contain repair work")
            if (
                self.effective_plan != self.initial_plan
                or self.change.grade != WeatherChangeGrade.NONE
            ):
                raise ValueError("no-action weather results must preserve the initial plan")
        elif not self.impacts or self.task is None or not self.attempts:
            raise ValueError("weather repair outcomes require impacts, a task, and attempts")
        expected_stop_reason = {
            WeatherRepairOutcome.NO_ACTION: WeatherRepairStopReason.NO_SIGNIFICANT_IMPACT,
            WeatherRepairOutcome.AUTO_APPLIED: WeatherRepairStopReason.FINALIZABLE,
            WeatherRepairOutcome.WAITING_FOR_USER: (
                WeatherRepairStopReason.USER_CONFIRMATION_REQUIRED
            ),
            WeatherRepairOutcome.UNRESOLVED: WeatherRepairStopReason.RETRY_LIMIT_REACHED,
        }[self.outcome]
        if self.stop_reason != expected_stop_reason:
            raise ValueError("weather repair stop reason must match its outcome")
        expected_confirmation = self.outcome == WeatherRepairOutcome.WAITING_FOR_USER
        if self.requires_user_confirmation != expected_confirmation:
            raise ValueError("weather HITL flag must match the repair outcome")
        if expected_confirmation:
            if self.proposed_plan is None or self.effective_plan != self.initial_plan:
                raise ValueError("HITL must retain the original plan and expose a proposal")
            if self.change.grade != WeatherChangeGrade.MAJOR:
                raise ValueError("HITL weather proposals must be major changes")
        if self.outcome == WeatherRepairOutcome.AUTO_APPLIED:
            if self.proposed_plan is None or self.effective_plan != self.proposed_plan:
                raise ValueError("auto-applied weather repairs must activate their proposal")
            if self.change.grade != WeatherChangeGrade.MINOR:
                raise ValueError("only minor weather changes may be auto-applied")
        if self.outcome == WeatherRepairOutcome.UNRESOLVED and (
            self.effective_plan != self.initial_plan or self.proposed_plan is not None
        ):
            raise ValueError("unresolved weather repairs must retain the initial plan")
        if self.outcome in {
            WeatherRepairOutcome.AUTO_APPLIED,
            WeatherRepairOutcome.WAITING_FOR_USER,
        } and (self.validation_report is None or not self.validation_report.can_finalize):
            raise ValueError("accepted weather proposals require a finalizable validation report")
        return self
