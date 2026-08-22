from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.planning.weather_repair_contracts import (
    WeatherChangeGrade,
    WeatherRepairOutcome,
    WeatherRepairStopReason,
)


class WeatherRepairScenario(StrEnum):
    NO_RISK = "no_risk"
    LOW_SEVERITY = "low_severity"
    INDOOR_ONLY = "indoor_only"
    OUTSIDE_TIME = "outside_time"
    ACTIVITY_TYPE_MISMATCH = "activity_type_mismatch"
    MINOR_LOCAL_REPLAN = "minor_local_replan"
    MAJOR_CROSS_DAY = "major_cross_day"
    SCOPE_VIOLATION = "scope_violation"
    PERSISTENT_FAILURE = "persistent_failure"
    IMPACT_REMAINS = "impact_remains"


class WeatherRepairExpectation(DomainModel):
    outcome: WeatherRepairOutcome
    stop_reason: WeatherRepairStopReason
    impact_count: int = Field(ge=0, le=4)
    attempt_count: int = Field(ge=0, le=2)
    change_grade: WeatherChangeGrade
    requires_user_confirmation: bool

    @model_validator(mode="after")
    def validate_expectation(self) -> "WeatherRepairExpectation":
        no_action = self.outcome == WeatherRepairOutcome.NO_ACTION
        if no_action != (self.impact_count == 0 and self.attempt_count == 0):
            raise ValueError("weather no-action expectations cannot contain repair work")
        if self.requires_user_confirmation != (
            self.outcome == WeatherRepairOutcome.WAITING_FOR_USER
        ):
            raise ValueError("weather expected HITL flag must match outcome")
        return self


class WeatherRepairEvalCase(DomainModel):
    case_id: Identifier
    title: NonEmptyText
    scenario: WeatherRepairScenario
    expected: WeatherRepairExpectation
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)


class WeatherRepairEvalSuite(DomainModel):
    suite: Literal["weather-repair-v1"] = "weather-repair-v1"
    version: Literal[1] = 1
    cases: tuple[WeatherRepairEvalCase, ...] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_inventory(self) -> "WeatherRepairEvalSuite":
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise ValueError("weather-repair case ids must be unique")
        if {item.scenario for item in self.cases} != set(WeatherRepairScenario):
            raise ValueError("weather-repair suite requires one case per scenario")
        return self


class WeatherRepairCaseResult(DomainModel):
    case_id: Identifier
    passed: bool
    expected_outcome: WeatherRepairOutcome
    actual_outcome: WeatherRepairOutcome
    expected_stop_reason: WeatherRepairStopReason
    actual_stop_reason: WeatherRepairStopReason
    expected_impact_count: int = Field(ge=0, le=4)
    actual_impact_count: int = Field(ge=0, le=4)
    expected_attempt_count: int = Field(ge=0, le=2)
    actual_attempt_count: int = Field(ge=0, le=2)
    expected_change_grade: WeatherChangeGrade
    actual_change_grade: WeatherChangeGrade
    expected_confirmation: bool
    actual_confirmation: bool
    task_created_proactively: bool
    source_traceable_impact_count: int = Field(ge=0, le=4)
    retry_bound_respected: bool
    effective_plan_preserved_for_hitl_or_failure: bool
    deterministic_replay: bool
    coordinator_model_call_count: Literal[0] = 0
    delegated_model_call_count: int = Field(ge=0, le=2)
    error_code: Identifier | None = None
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> "WeatherRepairCaseResult":
        if self.passed != all(item.passed for item in self.checks):
            raise ValueError("weather-repair case passed must equal all checks")
        if (self.error_code is None) != self.checks[0].passed:
            raise ValueError("first weather-repair check must represent workflow completion")
        if self.source_traceable_impact_count > self.actual_impact_count:
            raise ValueError("traceable weather impacts cannot exceed all impacts")
        return self


class WeatherRepairBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["weather-repair-v1"] = "weather-repair-v1"
    coordinator_version: Literal["weather-repair-v1"] = "weather-repair-v1"
    execution_mode: Literal["fixture"] = "fixture"
    dataset_sha256: Sha256Digest
    case_count: Literal[10] = 10
    passed_case_count: int = Field(ge=0, le=10)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    no_false_positive_case_count: int = Field(ge=0, le=5)
    proactive_task_case_count: int = Field(ge=0, le=5)
    auto_applied_case_count: int = Field(ge=0, le=1)
    hitl_case_count: int = Field(ge=0, le=1)
    bounded_retry_case_count: int = Field(ge=0, le=3)
    source_traceability_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    deterministic_replay_case_count: int = Field(ge=0, le=10)
    coordinator_model_call_count: Literal[0] = 0
    delegated_model_call_count: int = Field(ge=0)
    results: tuple[WeatherRepairCaseResult, ...] = Field(min_length=10, max_length=10)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aggregates(self) -> "WeatherRepairBaselineReport":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("weather-repair report case ids must be unique")
        aggregates = {
            "passed_case_count": sum(item.passed for item in self.results),
            "no_false_positive_case_count": sum(
                item.actual_outcome == WeatherRepairOutcome.NO_ACTION for item in self.results
            ),
            "proactive_task_case_count": sum(
                item.task_created_proactively for item in self.results
            ),
            "auto_applied_case_count": sum(
                item.actual_outcome == WeatherRepairOutcome.AUTO_APPLIED for item in self.results
            ),
            "hitl_case_count": sum(
                item.actual_outcome == WeatherRepairOutcome.WAITING_FOR_USER
                for item in self.results
            ),
            "bounded_retry_case_count": sum(
                item.actual_attempt_count == 2 for item in self.results
            ),
            "deterministic_replay_case_count": sum(
                item.deterministic_replay for item in self.results
            ),
            "coordinator_model_call_count": sum(
                item.coordinator_model_call_count for item in self.results
            ),
            "delegated_model_call_count": sum(
                item.delegated_model_call_count for item in self.results
            ),
        }
        for field_name, expected in aggregates.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name} must match weather-repair case results")
        if self.case_pass_rate != expected_rate(self.passed_case_count, self.case_count):
            raise ValueError("weather-repair case pass rate must match result counts")
        total_impacts = sum(item.actual_impact_count for item in self.results)
        traceable_impacts = sum(item.source_traceable_impact_count for item in self.results)
        if self.source_traceability_rate != expected_rate(traceable_impacts, total_impacts):
            raise ValueError("weather impact source rate must match result counts")
        return self
