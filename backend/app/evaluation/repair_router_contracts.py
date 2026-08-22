from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.validation import RepairAction, ResponsibleNode
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.planning.repair_contracts import RepairOutcome, RepairStopReason


class RepairFixtureSetup(StrEnum):
    REFERENCED_HARD_CASE = "referenced_hard_case"
    BUDGET_FLOOR = "budget_floor"


class RepairExecutorScenario(StrEnum):
    REPAIR = "repair"
    PERSISTENT_FAILURE = "persistent_failure"
    UNUSED = "unused"


class RepairRouterExpectation(DomainModel):
    outcome: RepairOutcome
    stop_reason: RepairStopReason
    final_can_finalize: bool
    attempt_actions: tuple[RepairAction, ...]
    executed_nodes_by_attempt: tuple[tuple[ResponsibleNode, ...], ...]
    pending_error_codes: tuple[NonEmptyText, ...]

    @model_validator(mode="after")
    def validate_expectation(self) -> "RepairRouterExpectation":
        if len(self.attempt_actions) != len(self.executed_nodes_by_attempt):
            raise ValueError("repair expected actions and node traces must have equal lengths")
        if any(len(nodes) != len(set(nodes)) for nodes in self.executed_nodes_by_attempt):
            raise ValueError("repair expected execution nodes must be unique per attempt")
        if len(self.pending_error_codes) != len(set(self.pending_error_codes)):
            raise ValueError("repair expected pending errors must be unique")
        if self.final_can_finalize != (self.stop_reason == RepairStopReason.FINALIZABLE):
            raise ValueError("repair expected stop reason must match finalization")
        return self


class RepairRouterEvalCase(DomainModel):
    case_id: Identifier
    title: NonEmptyText
    source_hard_validator_case_id: Identifier
    setup: RepairFixtureSetup = RepairFixtureSetup.REFERENCED_HARD_CASE
    executor_scenario: RepairExecutorScenario
    expected: RepairRouterExpectation
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)


class RepairRouterEvalSuite(DomainModel):
    suite: Literal["repair-router-v1"] = "repair-router-v1"
    version: Literal[1] = 1
    cases: tuple[RepairRouterEvalCase, ...] = Field(min_length=9, max_length=9)

    @model_validator(mode="after")
    def validate_inventory(self) -> "RepairRouterEvalSuite":
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise ValueError("repair-router case ids must be unique")
        return self


class RepairRouterCaseResult(DomainModel):
    case_id: Identifier
    passed: bool
    expected_outcome: RepairOutcome
    actual_outcome: RepairOutcome
    expected_stop_reason: RepairStopReason
    actual_stop_reason: RepairStopReason
    expected_attempt_actions: tuple[RepairAction, ...]
    actual_attempt_actions: tuple[RepairAction, ...]
    expected_executed_nodes: tuple[tuple[ResponsibleNode, ...], ...]
    actual_executed_nodes: tuple[tuple[ResponsibleNode, ...], ...]
    initial_error_codes: tuple[NonEmptyText, ...]
    expected_pending_error_codes: tuple[NonEmptyText, ...]
    actual_pending_error_codes: tuple[NonEmptyText, ...]
    retry_bound_respected: bool
    unaffected_nodes_reused: bool
    deterministic_replay: bool
    router_model_call_count: Literal[0] = 0
    delegated_model_call_count: int = Field(ge=0)
    error_code: Identifier | None = None
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> "RepairRouterCaseResult":
        if self.passed != all(item.passed for item in self.checks):
            raise ValueError("repair-router case passed must equal all checks")
        if (self.error_code is None) != self.checks[0].passed:
            raise ValueError("the first repair-router check must represent workflow completion")
        return self


class RepairRouterBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["repair-router-v1"] = "repair-router-v1"
    router_version: Literal["repair-router-v1"] = "repair-router-v1"
    execution_mode: Literal["fixture"] = "fixture"
    dataset_sha256: Sha256Digest
    case_count: Literal[9] = 9
    passed_case_count: int = Field(ge=0, le=9)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    exact_route_case_count: int = Field(ge=0, le=9)
    exact_route_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    retry_bound_case_count: int = Field(ge=0, le=9)
    unaffected_reuse_case_count: int = Field(ge=0, le=9)
    deterministic_replay_case_count: int = Field(ge=0, le=9)
    total_repair_attempt_count: int = Field(ge=0)
    router_model_call_count: Literal[0] = 0
    delegated_model_call_count: int = Field(ge=0)
    results: tuple[RepairRouterCaseResult, ...] = Field(min_length=9, max_length=9)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aggregates(self) -> "RepairRouterBaselineReport":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("repair-router report case ids must be unique")
        aggregates = {
            "passed_case_count": sum(item.passed for item in self.results),
            "exact_route_case_count": sum(
                item.actual_attempt_actions == item.expected_attempt_actions
                and item.actual_executed_nodes == item.expected_executed_nodes
                for item in self.results
            ),
            "retry_bound_case_count": sum(item.retry_bound_respected for item in self.results),
            "unaffected_reuse_case_count": sum(
                item.unaffected_nodes_reused for item in self.results
            ),
            "deterministic_replay_case_count": sum(
                item.deterministic_replay for item in self.results
            ),
            "total_repair_attempt_count": sum(
                len(item.actual_attempt_actions) for item in self.results
            ),
            "router_model_call_count": sum(item.router_model_call_count for item in self.results),
            "delegated_model_call_count": sum(
                item.delegated_model_call_count for item in self.results
            ),
        }
        for field_name, aggregate_expected in aggregates.items():
            if getattr(self, field_name) != aggregate_expected:
                raise ValueError(f"{field_name} must match repair-router case results")
        rates = {
            "case_pass_rate": expected_rate(self.passed_case_count, self.case_count),
            "exact_route_rate": expected_rate(self.exact_route_case_count, self.case_count),
        }
        for field_name, rate_expected in rates.items():
            if getattr(self, field_name) != rate_expected:
                raise ValueError(f"{field_name} must match repair-router aggregate counts")
        return self
