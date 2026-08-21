from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerCapability, PlannerReadiness
from app.domain.provider import ProviderFailure
from app.domain.request import ConstraintStrength, TripRequest
from app.domain.sources import DataMode
from app.domain.workflow import PlanningWorkflowStatus

RATE_QUANTUM = Decimal("0.0001")


def expected_rate(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal("1.0000")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        RATE_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


class SeedTier(StrEnum):
    STANDARD = "standard"
    HARD = "hard"


class SeedProviderBehavior(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    FORBIDDEN = "forbidden"


class ExpectedPOISearchCall(DomainModel):
    keywords: NonEmptyText
    city_adcode: str = Field(pattern=r"^\d{6}$")
    limit: int = Field(default=1, ge=1, le=3)


class PlanningSeedProviderSpec(DomainModel):
    behavior: SeedProviderBehavior
    expected_calls: tuple[ExpectedPOISearchCall, ...] = ()
    candidates: tuple[CandidatePOI, ...] = ()
    failure: ProviderFailure | None = None

    @model_validator(mode="after")
    def validate_behavior_payload(self) -> "PlanningSeedProviderSpec":
        if any(candidate.source.data_mode != DataMode.FIXTURE for candidate in self.candidates):
            raise ValueError("seed candidates must be explicitly labelled as fixture data")
        if self.behavior == SeedProviderBehavior.SUCCESS:
            if not self.expected_calls or not self.candidates or self.failure is not None:
                raise ValueError("success provider requires calls and candidates without failure")
        elif self.behavior == SeedProviderBehavior.FAILURE:
            if not self.expected_calls or self.candidates or self.failure is None:
                raise ValueError(
                    "failure provider requires calls and one failure without candidates"
                )
        elif self.expected_calls or self.candidates or self.failure is not None:
            raise ValueError("forbidden provider cannot define calls, candidates, or failure")
        return self


class ExpectedConstraintBuckets(DomainModel):
    confirmed_hard: tuple[Identifier, ...] = ()
    confirmed_soft: tuple[Identifier, ...] = ()
    pending: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_unique_constraint_ids(self) -> "ExpectedConstraintBuckets":
        all_ids = (*self.confirmed_hard, *self.confirmed_soft, *self.pending)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("expected constraint ids must appear in exactly one bucket")
        return self


class PlanningSeedExpectation(DomainModel):
    status: PlanningWorkflowStatus
    readiness: PlannerReadiness
    ready_capabilities: tuple[PlannerCapability, ...]
    blocked_capabilities: tuple[PlannerCapability, ...]
    candidate_count: int = Field(ge=0)
    candidate_names: tuple[NonEmptyText, ...] = ()
    constraint_buckets: ExpectedConstraintBuckets

    @model_validator(mode="after")
    def validate_expected_partitions(self) -> "PlanningSeedExpectation":
        ready = set(self.ready_capabilities)
        blocked = set(self.blocked_capabilities)
        if ready & blocked or ready | blocked != set(PlannerCapability):
            raise ValueError("expected capabilities must form a complete non-overlapping partition")
        if len(self.ready_capabilities) != len(ready) or len(self.blocked_capabilities) != len(
            blocked
        ):
            raise ValueError("expected capability lists cannot contain duplicates")
        if self.candidate_count != len(self.candidate_names):
            raise ValueError("candidate_count must match candidate_names")
        return self


class FutureExpectation(DomainModel):
    code: Identifier
    description: NonEmptyText


class PlanningSeedCase(DomainModel):
    version: Literal[1] = 1
    case_id: Identifier
    tier: SeedTier
    title: NonEmptyText
    request: TripRequest
    provider: PlanningSeedProviderSpec
    expected: PlanningSeedExpectation
    future_expectations: tuple[FutureExpectation, ...] = ()
    boundary_notes: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_semantics(self) -> "PlanningSeedCase":
        if not self.case_id.startswith(f"seed-{self.tier.value}-") or not self.case_id.endswith(
            "-v1"
        ):
            raise ValueError("case_id must encode the tier and version")

        actual_hard = tuple(
            item.constraint_id
            for item in self.request.constraints.items
            if item.confirmed and item.strength == ConstraintStrength.HARD
        )
        actual_soft = tuple(
            item.constraint_id
            for item in self.request.constraints.items
            if item.confirmed and item.strength == ConstraintStrength.SOFT
        )
        actual_pending = tuple(
            item.constraint_id for item in self.request.constraints.items if not item.confirmed
        )
        buckets = self.expected.constraint_buckets
        if (
            buckets.confirmed_hard != actual_hard
            or buckets.confirmed_soft != actual_soft
            or buckets.pending != actual_pending
        ):
            raise ValueError("expected constraint buckets must match the TripRequest")

        if self.provider.behavior == SeedProviderBehavior.SUCCESS:
            if self.expected.status != PlanningWorkflowStatus.CANDIDATES_READY:
                raise ValueError("success provider cases must expect candidates_ready")
            candidate_names = tuple(candidate.name for candidate in self.provider.candidates)
            if self.expected.candidate_names != candidate_names:
                raise ValueError("expected candidate names must match provider candidates")
        elif self.provider.behavior == SeedProviderBehavior.FAILURE:
            if self.expected.status != PlanningWorkflowStatus.PROVIDER_FAILED:
                raise ValueError("failure provider cases must expect provider_failed")
            if self.expected.candidate_count != 0:
                raise ValueError("failure seed cases cannot expect fabricated candidates")
        elif self.expected.status not in {
            PlanningWorkflowStatus.NEEDS_CLARIFICATION,
            PlanningWorkflowStatus.NO_CANDIDATE_QUERY,
        }:
            raise ValueError("forbidden provider cases must stop or skip before provider access")
        return self


class PlanningSeedManifestEntry(DomainModel):
    case_id: Identifier
    tier: SeedTier
    path: NonEmptyText


class PlanningSeedManifest(DomainModel):
    suite: Literal["planning-seed-v1"] = "planning-seed-v1"
    version: Literal[1] = 1
    status: Literal["executable_baseline"] = "executable_baseline"
    case_schema: Literal["../../schemas/planning-seed-case.v1.json"] = (
        "../../schemas/planning-seed-case.v1.json"
    )
    cases: tuple[PlanningSeedManifestEntry, ...] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_case_inventory(self) -> "PlanningSeedManifest":
        ids = [item.case_id for item in self.cases]
        paths = [item.path for item in self.cases]
        if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise ValueError("manifest case ids and paths must be unique")
        if sum(item.tier == SeedTier.STANDARD for item in self.cases) != 6:
            raise ValueError("manifest must contain six standard cases")
        if sum(item.tier == SeedTier.HARD for item in self.cases) != 4:
            raise ValueError("manifest must contain four hard cases")
        return self


class EvaluationCheck(DomainModel):
    code: Identifier
    passed: bool


class PlanningSeedCaseResult(DomainModel):
    case_id: Identifier
    tier: SeedTier
    passed: bool
    expected_status: PlanningWorkflowStatus
    actual_status: PlanningWorkflowStatus
    provider_call_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    traceable_candidate_count: int = Field(ge=0)
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_result(self) -> "PlanningSeedCaseResult":
        if self.traceable_candidate_count > self.candidate_count:
            raise ValueError("traceable candidates cannot exceed total candidates")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("case passed must equal the conjunction of checks")
        return self


class PlanningSeedBaselineReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["planning-seed-v1"] = "planning-seed-v1"
    workflow_version: Literal["minimal-planning-graph-v1"] = "minimal-planning-graph-v1"
    dataset_sha256: Sha256Digest
    case_count: Literal[10] = 10
    standard_case_count: Literal[6] = 6
    hard_case_count: Literal[4] = 4
    passed_case_count: int = Field(ge=0, le=10)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    check_count: int = Field(ge=1)
    passed_check_count: int = Field(ge=0)
    check_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    candidate_count: int = Field(ge=0)
    traceable_candidate_count: int = Field(ge=0)
    source_traceability_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    results: tuple[PlanningSeedCaseResult, ...] = Field(min_length=10, max_length=10)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report_aggregates(self) -> "PlanningSeedBaselineReport":
        result_ids = [item.case_id for item in self.results]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("baseline result case ids must be unique")
        if sum(item.tier == SeedTier.STANDARD for item in self.results) != 6:
            raise ValueError("baseline report must contain six standard results")
        if sum(item.tier == SeedTier.HARD for item in self.results) != 4:
            raise ValueError("baseline report must contain four hard results")
        if self.passed_case_count != sum(item.passed for item in self.results):
            raise ValueError("passed_case_count must match case results")
        checks = [check for item in self.results for check in item.checks]
        if self.check_count != len(checks):
            raise ValueError("check_count must match case result checks")
        if self.passed_check_count != sum(check.passed for check in checks):
            raise ValueError("passed_check_count must match check results")
        if self.candidate_count != sum(item.candidate_count for item in self.results):
            raise ValueError("candidate_count must match case results")
        if self.traceable_candidate_count != sum(
            item.traceable_candidate_count for item in self.results
        ):
            raise ValueError("traceable_candidate_count must match case results")
        expected_case_rate = expected_rate(self.passed_case_count, self.case_count)
        expected_check_rate = expected_rate(self.passed_check_count, self.check_count)
        expected_source_rate = expected_rate(
            self.traceable_candidate_count,
            self.candidate_count,
        )
        if self.case_pass_rate != expected_case_rate:
            raise ValueError("case_pass_rate must match aggregate counts")
        if self.check_pass_rate != expected_check_rate:
            raise ValueError("check_pass_rate must match aggregate counts")
        if self.source_traceability_rate != expected_source_rate:
            raise ValueError("source_traceability_rate must match aggregate counts")
        return self
