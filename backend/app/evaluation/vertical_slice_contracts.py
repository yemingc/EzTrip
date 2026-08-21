from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.agents.contracts import PlannerProposalBatch
from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.candidates import CandidatePOI
from app.domain.money import CostItem
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.validation import BudgetAssessmentStatus, PlanValidationStatus
from app.evaluation.contracts import EvaluationCheck, ExpectedPOISearchCall, expected_rate
from app.planning.vertical_slice import VerticalSliceOutcome


class VerticalSlicePOIResponse(DomainModel):
    request: ExpectedPOISearchCall
    candidates: tuple[CandidatePOI, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_fixture_sources(self) -> "VerticalSlicePOIResponse":
        if any(candidate.source.data_mode != DataMode.FIXTURE for candidate in self.candidates):
            raise ValueError("Gate 2 POI candidates must be labelled fixture data")
        return self


class VerticalSliceExpected(DomainModel):
    outcome: VerticalSliceOutcome
    validation_status: PlanValidationStatus
    budget_status: BudgetAssessmentStatus
    can_finalize: bool
    issue_rule_codes: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_expected_outcome(self) -> "VerticalSliceExpected":
        is_conflicted = self.validation_status == PlanValidationStatus.CONFLICTED
        if (self.outcome == VerticalSliceOutcome.CONFLICTED) != is_conflicted:
            raise ValueError("expected outcome must follow validation_status")
        if self.can_finalize == is_conflicted:
            raise ValueError("expected can_finalize must be false exactly for conflicts")
        if len(self.issue_rule_codes) != len(set(self.issue_rule_codes)):
            raise ValueError("expected issue rule codes must be unique")
        return self


class VerticalSliceCase(DomainModel):
    version: Literal[1] = 1
    case_id: Identifier
    title: NonEmptyText
    request: TripRequest
    provider_responses: tuple[VerticalSlicePOIResponse, ...] = Field(
        min_length=1,
        max_length=5,
    )
    planner_proposal: PlannerProposalBatch
    planner_model: Literal["fixture-planner-gate2-v1"] = "fixture-planner-gate2-v1"
    cost_items: tuple[CostItem, ...] = Field(min_length=1)
    expected: VerticalSliceExpected
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_lineage(self) -> "VerticalSliceCase":
        if not self.case_id.startswith("gate2-beijing-") or not self.case_id.endswith("-v1"):
            raise ValueError("Gate 2 case_id must encode Beijing scope and version")
        if self.request.destination_city not in {"北京", "北京市"} or self.request.day_count != 3:
            raise ValueError("Gate 2 suite is frozen to a three-day Beijing request")
        candidates = tuple(
            candidate for response in self.provider_responses for candidate in response.candidates
        )
        candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Gate 2 provider responses must not repeat candidate ids")
        proposal_ids = tuple(item.candidate_id for item in self.planner_proposal.items)
        if len(proposal_ids) != len(set(proposal_ids)) or set(proposal_ids) != set(candidate_ids):
            raise ValueError("Gate 2 Planner proposal must cover the provider candidates exactly")
        cost_ids = tuple(item.cost_item_id for item in self.cost_items)
        if len(cost_ids) != len(set(cost_ids)):
            raise ValueError("Gate 2 cost item ids must be unique")
        if any(item.source.data_mode != DataMode.FIXTURE for item in self.cost_items):
            raise ValueError("Gate 2 costs must be explicitly labelled fixture data")
        return self


class VerticalSliceSuite(DomainModel):
    suite: Literal["beijing-three-day-vertical-slice-gate2-v1"] = (
        "beijing-three-day-vertical-slice-gate2-v1"
    )
    version: Literal[1] = 1
    execution_mode: Literal["fixture"] = "fixture"
    cases: tuple[VerticalSliceCase, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_case_inventory(self) -> "VerticalSliceSuite":
        if len({case.case_id for case in self.cases}) != 2:
            raise ValueError("Gate 2 suite requires two unique cases")
        if len({case.request.request_id for case in self.cases}) != 2:
            raise ValueError("Gate 2 requests must have unique request ids")
        outcomes = {case.expected.outcome for case in self.cases}
        if outcomes != {VerticalSliceOutcome.READY, VerticalSliceOutcome.CONFLICTED}:
            raise ValueError("Gate 2 suite requires one ready and one conflicted case")
        return self


class VerticalSliceCaseResult(DomainModel):
    case_id: Identifier
    passed: bool
    outcome: VerticalSliceOutcome
    validation_status: PlanValidationStatus
    budget_status: BudgetAssessmentStatus
    can_finalize: bool
    provider_call_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    scheduled_candidate_count: int = Field(ge=0)
    traceable_candidate_count: int = Field(ge=0)
    day_count: int = Field(ge=0, le=5)
    budget_total_minimum: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    budget_total_maximum: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    budget_minimum_gap: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    issue_rule_codes: tuple[NonEmptyText, ...]
    deterministic_replay_match: bool
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_aggregates(self) -> "VerticalSliceCaseResult":
        if self.scheduled_candidate_count > self.candidate_count:
            raise ValueError("scheduled candidates cannot exceed provider candidates")
        if self.traceable_candidate_count > self.candidate_count:
            raise ValueError("traceable candidates cannot exceed provider candidates")
        if self.budget_total_maximum < self.budget_total_minimum:
            raise ValueError("budget maximum cannot be below minimum")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("case passed must equal the conjunction of checks")
        return self


class VerticalSliceGateReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["beijing-three-day-vertical-slice-gate2-v1"] = (
        "beijing-three-day-vertical-slice-gate2-v1"
    )
    workflow_version: Literal["beijing-three-day-vertical-slice-v1"] = (
        "beijing-three-day-vertical-slice-v1"
    )
    execution_mode: Literal["fixture"] = "fixture"
    model: Literal["fixture-planner-gate2-v1"] = "fixture-planner-gate2-v1"
    dataset_sha256: Sha256Digest
    case_count: Literal[2] = 2
    passed_case_count: int = Field(ge=0, le=2)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    check_count: int = Field(ge=1)
    passed_check_count: int = Field(ge=0)
    check_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    candidate_count: int = Field(ge=0)
    traceable_candidate_count: int = Field(ge=0)
    source_traceability_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    deterministic_replay_count: int = Field(ge=0, le=2)
    results: tuple[VerticalSliceCaseResult, ...] = Field(min_length=2, max_length=2)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report_aggregates(self) -> "VerticalSliceGateReport":
        if len({result.case_id for result in self.results}) != self.case_count:
            raise ValueError("Gate 2 report case ids must be unique")
        checks = tuple(check for result in self.results for check in result.checks)
        aggregates = {
            "passed_case_count": sum(result.passed for result in self.results),
            "check_count": len(checks),
            "passed_check_count": sum(check.passed for check in checks),
            "candidate_count": sum(result.candidate_count for result in self.results),
            "traceable_candidate_count": sum(
                result.traceable_candidate_count for result in self.results
            ),
            "deterministic_replay_count": sum(
                result.deterministic_replay_match for result in self.results
            ),
        }
        for field_name, expected in aggregates.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name} must match case results")
        rates = {
            "case_pass_rate": expected_rate(self.passed_case_count, self.case_count),
            "check_pass_rate": expected_rate(self.passed_check_count, self.check_count),
            "source_traceability_rate": expected_rate(
                self.traceable_candidate_count,
                self.candidate_count,
            ),
        }
        for field_name, rate_expected in rates.items():
            if getattr(self, field_name) != rate_expected:
                raise ValueError(f"{field_name} must match aggregate counts")
        return self
