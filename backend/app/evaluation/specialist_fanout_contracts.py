from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.provider import ProviderErrorCategory
from app.domain.request import TripRequest
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.planning.specialist_contracts import (
    SpecialistBranchStatus,
    SpecialistFanoutStatus,
    SpecialistName,
)


class SpecialistBranchExpectation(DomainModel):
    specialist: SpecialistName
    status: SpecialistBranchStatus
    provider_failure_category: ProviderErrorCategory | None = None

    @model_validator(mode="after")
    def validate_failure_expectation(self) -> "SpecialistBranchExpectation":
        if (self.status == SpecialistBranchStatus.FAILED) != (
            self.provider_failure_category is not None
        ):
            raise ValueError("only failed branches expect a Provider failure category")
        return self


class SpecialistFanoutExpectation(DomainModel):
    status: SpecialistFanoutStatus
    branches: tuple[SpecialistBranchExpectation, ...] = Field(min_length=3, max_length=3)
    require_parallel_provider_entry: bool = False

    @model_validator(mode="after")
    def validate_branch_inventory(self) -> "SpecialistFanoutExpectation":
        if tuple(item.specialist for item in self.branches) != tuple(SpecialistName):
            raise ValueError("specialist expectations require one ordered branch per specialist")
        succeeded = sum(item.status == SpecialistBranchStatus.SUCCEEDED for item in self.branches)
        failed = sum(item.status == SpecialistBranchStatus.FAILED for item in self.branches)
        expected_status = SpecialistFanoutStatus.PARTIAL
        if succeeded == len(self.branches):
            expected_status = SpecialistFanoutStatus.COMPLETE
        elif succeeded == 0 and failed == 0:
            expected_status = SpecialistFanoutStatus.BLOCKED
        elif succeeded == 0:
            expected_status = SpecialistFanoutStatus.FAILED
        if self.status != expected_status:
            raise ValueError("expected fan-out status must match expected branch outcomes")
        if self.require_parallel_provider_entry and self.status != SpecialistFanoutStatus.COMPLETE:
            raise ValueError("parallel-entry evidence is reserved for the complete control case")
        return self


class SpecialistFanoutEvalCase(DomainModel):
    case_id: Identifier
    title: NonEmptyText
    request: TripRequest
    explore_fixture_case_id: Identifier
    stay_fixture_case_id: Identifier
    injected_provider_failure: SpecialistName | None = None
    expected: SpecialistFanoutExpectation
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_contract(self) -> "SpecialistFanoutEvalCase":
        if not self.case_id.startswith("specialist-fanout-") or not self.case_id.endswith("-v1"):
            raise ValueError("specialist fan-out case_id must encode the suite and version")
        failed = tuple(
            item.specialist
            for item in self.expected.branches
            if item.status == SpecialistBranchStatus.FAILED
        )
        if self.injected_provider_failure is None and failed:
            raise ValueError("failed branches require an injected Provider failure")
        if self.injected_provider_failure is not None and failed != (
            self.injected_provider_failure,
        ):
            raise ValueError("injected Provider failure must match the failed branch")
        if self.injected_provider_failure == SpecialistName.STAY:
            raise ValueError("V1 reserves Stay for the deterministic capability-skip case")
        return self


class SpecialistFanoutEvalSuite(DomainModel):
    suite: Literal["specialist-fanout-v1"] = "specialist-fanout-v1"
    version: Literal[1] = 1
    data_mode: Literal["fixture"] = "fixture"
    cases: tuple[SpecialistFanoutEvalCase, ...] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def validate_suite_inventory(self) -> "SpecialistFanoutEvalSuite":
        case_ids = [item.case_id for item in self.cases]
        request_ids = [item.request.request_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("specialist fan-out case ids must be unique")
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("specialist fan-out request ids must be unique")
        status_counts = {
            status: sum(item.expected.status == status for item in self.cases)
            for status in SpecialistFanoutStatus
        }
        if status_counts != {
            SpecialistFanoutStatus.COMPLETE: 1,
            SpecialistFanoutStatus.PARTIAL: 3,
            SpecialistFanoutStatus.BLOCKED: 1,
            SpecialistFanoutStatus.FAILED: 0,
        }:
            raise ValueError("specialist fan-out V1 requires 1 complete, 3 partial, 1 blocked")
        if sum(item.injected_provider_failure is not None for item in self.cases) != 2:
            raise ValueError("specialist fan-out V1 requires two Provider failure injections")
        if sum(item.expected.require_parallel_provider_entry for item in self.cases) != 1:
            raise ValueError("specialist fan-out V1 requires one parallel-entry control case")
        return self


class SpecialistFanoutCaseResult(DomainModel):
    case_id: Identifier
    expected_status: SpecialistFanoutStatus
    actual_status: SpecialistFanoutStatus | None = None
    passed: bool
    expected_branch_count: Literal[3] = 3
    actual_branch_count: int = Field(ge=0, le=3)
    branch_status_match_count: int = Field(ge=0, le=3)
    exact_ordered_merge: bool
    typed_provider_failure_count: int = Field(ge=0, le=1)
    preserved_success_count: int = Field(ge=0, le=2)
    proactive_weather_call_count: int = Field(ge=0, le=1)
    model_call_count: int = Field(ge=0, le=4)
    provider_call_count: int = Field(ge=0, le=9)
    usage_call_count: int = Field(ge=0, le=4)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    parallel_provider_peak: int = Field(ge=0, le=3)
    source_traceability_passed: bool
    fanout_latency_ms: int = Field(ge=0)
    branch_latency_sum_ms: int = Field(ge=0)
    error_code: Identifier | None = None
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_result(self) -> "SpecialistFanoutCaseResult":
        if self.branch_status_match_count > self.actual_branch_count:
            raise ValueError("branch status matches cannot exceed actual branches")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("specialist fan-out case passed must equal the conjunction of checks")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("case total_tokens must equal prompt_tokens + completion_tokens")
        return self


class SpecialistFanoutBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["specialist-fanout-v1"] = "specialist-fanout-v1"
    workflow_version: Literal["specialist-fanout-v1"] = "specialist-fanout-v1"
    execution_mode: Literal["fixture", "live"]
    explore_model: NonEmptyText
    stay_model: NonEmptyText
    dataset_sha256: Sha256Digest
    case_count: Literal[5] = 5
    passed_case_count: int = Field(ge=0, le=5)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    branch_expectation_count: Literal[15] = 15
    branch_status_match_count: int = Field(ge=0, le=15)
    branch_status_accuracy: Decimal = Field(ge=0, le=1, decimal_places=4)
    exact_ordered_merge_case_count: int = Field(ge=0, le=5)
    typed_provider_failure_count: int = Field(ge=0, le=2)
    preserved_success_count: int = Field(ge=0, le=4)
    proactive_weather_call_count: int = Field(ge=0, le=4)
    blocked_zero_call_case_count: int = Field(ge=0, le=1)
    parallel_provider_entry_case_count: int = Field(ge=0, le=1)
    source_traceability_case_count: int = Field(ge=0, le=4)
    model_call_count: int = Field(ge=0, le=20)
    provider_call_count: int = Field(ge=0, le=45)
    usage_call_count: int = Field(ge=0, le=20)
    total_prompt_tokens: int = Field(ge=0)
    total_completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    p50_fanout_latency_ms: int = Field(ge=0)
    p95_fanout_latency_ms: int = Field(ge=0)
    results: tuple[SpecialistFanoutCaseResult, ...] = Field(min_length=5, max_length=5)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report_aggregates(self) -> "SpecialistFanoutBaselineReport":
        if len({item.case_id for item in self.results}) != self.case_count:
            raise ValueError("specialist fan-out report case ids must be unique")
        aggregates = {
            "passed_case_count": sum(item.passed for item in self.results),
            "branch_status_match_count": sum(
                item.branch_status_match_count for item in self.results
            ),
            "exact_ordered_merge_case_count": sum(
                item.exact_ordered_merge for item in self.results
            ),
            "typed_provider_failure_count": sum(
                item.typed_provider_failure_count for item in self.results
            ),
            "preserved_success_count": sum(item.preserved_success_count for item in self.results),
            "proactive_weather_call_count": sum(
                item.proactive_weather_call_count for item in self.results
            ),
            "source_traceability_case_count": sum(
                item.actual_status != SpecialistFanoutStatus.BLOCKED
                and item.source_traceability_passed
                for item in self.results
            ),
            "model_call_count": sum(item.model_call_count for item in self.results),
            "provider_call_count": sum(item.provider_call_count for item in self.results),
            "usage_call_count": sum(item.usage_call_count for item in self.results),
            "total_prompt_tokens": sum(item.prompt_tokens for item in self.results),
            "total_completion_tokens": sum(item.completion_tokens for item in self.results),
            "total_tokens": sum(item.total_tokens for item in self.results),
        }
        for field_name, expected in aggregates.items():
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name} must match specialist fan-out case results")
        if self.case_pass_rate != expected_rate(self.passed_case_count, self.case_count):
            raise ValueError("case_pass_rate must match specialist fan-out case results")
        if self.branch_status_accuracy != expected_rate(
            self.branch_status_match_count,
            self.branch_expectation_count,
        ):
            raise ValueError("branch_status_accuracy must match specialist fan-out results")
        blocked_zero_call_count = sum(
            item.actual_status == SpecialistFanoutStatus.BLOCKED
            and item.model_call_count == 0
            and item.provider_call_count == 0
            for item in self.results
        )
        if self.blocked_zero_call_case_count != blocked_zero_call_count:
            raise ValueError("blocked_zero_call_case_count must match case results")
        parallel_entry_count = sum(item.parallel_provider_peak == 3 for item in self.results)
        if self.parallel_provider_entry_case_count != parallel_entry_count:
            raise ValueError("parallel_provider_entry_case_count must match case results")
        latencies = sorted(item.fanout_latency_ms for item in self.results)
        if self.p50_fanout_latency_ms != nearest_rank(latencies, 50):
            raise ValueError("p50_fanout_latency_ms must match case results")
        if self.p95_fanout_latency_ms != nearest_rank(latencies, 95):
            raise ValueError("p95_fanout_latency_ms must match case results")
        return self


def nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    rank = (percentile * len(values) + 99) // 100
    return values[max(rank - 1, 0)]
