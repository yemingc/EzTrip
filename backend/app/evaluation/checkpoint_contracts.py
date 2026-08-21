import hashlib
import json
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.planning.stateful_contracts import (
    HumanReviewAction,
    HumanReviewKind,
    PlanningThreadStatus,
    StatefulPlanningNodeName,
)


class CheckpointHitlCase(DomainModel):
    version: Literal[1] = 1
    case_id: Identifier
    source_case_id: Identifier
    thread_id: Identifier
    action: HumanReviewAction
    reviewer_id: Identifier
    comment: NonEmptyText
    decided_at: AwareDatetime
    expected_review_kind: HumanReviewKind
    expected_terminal_status: PlanningThreadStatus

    @model_validator(mode="after")
    def validate_expected_transition(self) -> "CheckpointHitlCase":
        expected = {
            HumanReviewKind.PLAN_APPROVAL: (
                HumanReviewAction.APPROVE_DRAFT,
                PlanningThreadStatus.APPROVED_DRAFT,
            ),
            HumanReviewKind.CONFLICT_RESOLUTION: (
                HumanReviewAction.ACKNOWLEDGE_CONFLICT,
                PlanningThreadStatus.CONFLICT_ACKNOWLEDGED,
            ),
        }[self.expected_review_kind]
        if (self.action, self.expected_terminal_status) != expected:
            raise ValueError("checkpoint HITL action and terminal status must follow review kind")
        return self


class CheckpointHitlSuite(DomainModel):
    suite: Literal["stateful-checkpoint-hitl-v1"] = "stateful-checkpoint-hitl-v1"
    version: Literal[1] = 1
    execution_mode: Literal["fixture"] = "fixture"
    source_suite: Literal["beijing-three-day-vertical-slice-gate2-v1"] = (
        "beijing-three-day-vertical-slice-gate2-v1"
    )
    source_dataset_sha256: Sha256Digest
    cases: tuple[CheckpointHitlCase, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_inventory(self) -> "CheckpointHitlSuite":
        if len({case.case_id for case in self.cases}) != 2:
            raise ValueError("checkpoint HITL suite requires two unique case ids")
        if len({case.source_case_id for case in self.cases}) != 2:
            raise ValueError("checkpoint HITL suite requires two unique source cases")
        if len({case.thread_id for case in self.cases}) != 2:
            raise ValueError("checkpoint HITL suite requires isolated thread ids")
        if {case.expected_review_kind for case in self.cases} != set(HumanReviewKind):
            raise ValueError("checkpoint HITL suite requires approval and conflict review cases")
        return self


def checkpoint_hitl_dataset_sha256(suite: CheckpointHitlSuite) -> str:
    canonical = json.dumps(
        suite.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CheckpointHitlCaseResult(DomainModel):
    case_id: Identifier
    source_case_id: Identifier
    passed: bool
    paused_status: PlanningThreadStatus
    review_kind: HumanReviewKind
    action: HumanReviewAction
    terminal_status: PlanningThreadStatus
    provider_call_count: int = Field(ge=0)
    planner_model_call_count: int = Field(ge=0)
    restored_provider_call_count: int = Field(ge=0)
    restored_planner_model_call_count: int = Field(ge=0)
    checkpoint_count: int = Field(ge=1)
    state_restored_after_runtime_rebuild: bool
    vertical_slice_preserved: bool
    event_nodes: tuple[StatefulPlanningNodeName, ...]
    checks: tuple[EvaluationCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aggregates(self) -> "CheckpointHitlCaseResult":
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("checkpoint HITL case passed must equal its checks")
        return self


class CheckpointHitlReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    suite: Literal["stateful-checkpoint-hitl-v1"] = "stateful-checkpoint-hitl-v1"
    workflow_version: Literal["stateful-planning-checkpoint-v1"] = "stateful-planning-checkpoint-v1"
    execution_mode: Literal["fixture"] = "fixture"
    checkpoint_backend: Literal["sqlite"] = "sqlite"
    source_dataset_sha256: Sha256Digest
    dataset_sha256: Sha256Digest
    case_count: Literal[2] = 2
    passed_case_count: int = Field(ge=0, le=2)
    case_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    check_count: int = Field(ge=1)
    passed_check_count: int = Field(ge=0)
    check_pass_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    runtime_reconstruction_count: int = Field(ge=0, le=2)
    no_expensive_replay_count: int = Field(ge=0, le=2)
    draft_preserved_count: int = Field(ge=0, le=2)
    results: tuple[CheckpointHitlCaseResult, ...] = Field(min_length=2, max_length=2)
    limitations: tuple[NonEmptyText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report_aggregates(self) -> "CheckpointHitlReport":
        if len({result.case_id for result in self.results}) != self.case_count:
            raise ValueError("checkpoint HITL report case ids must be unique")
        checks = tuple(check for result in self.results for check in result.checks)
        aggregates = {
            "passed_case_count": sum(result.passed for result in self.results),
            "check_count": len(checks),
            "passed_check_count": sum(check.passed for check in checks),
            "runtime_reconstruction_count": sum(
                result.state_restored_after_runtime_rebuild for result in self.results
            ),
            "no_expensive_replay_count": sum(
                result.restored_provider_call_count == 0
                and result.restored_planner_model_call_count == 0
                for result in self.results
            ),
            "draft_preserved_count": sum(
                result.vertical_slice_preserved for result in self.results
            ),
        }
        for field_name, expected_count in aggregates.items():
            if getattr(self, field_name) != expected_count:
                raise ValueError(f"{field_name} must match checkpoint HITL case results")
        rates = {
            "case_pass_rate": expected_rate(self.passed_case_count, self.case_count),
            "check_pass_rate": expected_rate(self.passed_check_count, self.check_count),
        }
        for field_name, expected_rate_value in rates.items():
            if getattr(self, field_name) != expected_rate_value:
                raise ValueError(f"{field_name} must match checkpoint HITL aggregate counts")
        return self
