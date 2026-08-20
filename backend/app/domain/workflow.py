from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerCapability, PlannerContext
from app.domain.provider import ProviderFailure
from app.domain.request import ConstraintKind
from app.domain.sources import DataMode


class PlanningWorkflowStatus(StrEnum):
    NEEDS_CLARIFICATION = "needs_clarification"
    NO_CANDIDATE_QUERY = "no_candidate_query"
    CANDIDATES_READY = "candidates_ready"
    PROVIDER_FAILED = "provider_failed"


class CandidateQuerySource(StrEnum):
    CONFIRMED_MUST_VISIT = "confirmed_must_visit"


class PlanningNodeName(StrEnum):
    COMPILE_CONTEXT = "compile_context"
    CLARIFICATION_GATE = "clarification_gate"
    CANDIDATE_SEARCH = "candidate_search"


class PlanningNodeOutcome(StrEnum):
    COMPILED = "compiled"
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


class CandidateSearchQuery(DomainModel):
    query_id: Identifier
    keywords: NonEmptyText
    city_adcode: str = Field(pattern=r"^\d{6}$")
    limit: int = Field(default=1, ge=1, le=3)
    source: Literal[CandidateQuerySource.CONFIRMED_MUST_VISIT] = (
        CandidateQuerySource.CONFIRMED_MUST_VISIT
    )
    source_constraint_id: Identifier
    requested_value: NonEmptyText


class PlanningNodeEvent(DomainModel):
    node: PlanningNodeName
    outcome: PlanningNodeOutcome
    detail: NonEmptyText


class MinimalPlanningResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    workflow_version: Literal["minimal-planning-graph-v1"] = "minimal-planning-graph-v1"
    request_id: Identifier
    data_mode: Literal[DataMode.LIVE, DataMode.FIXTURE]
    planner_context: PlannerContext
    status: PlanningWorkflowStatus
    candidate_queries: tuple[CandidateSearchQuery, ...] = ()
    candidates: tuple[CandidatePOI, ...] = ()
    provider_failures: tuple[ProviderFailure, ...] = ()
    events: tuple[PlanningNodeEvent, ...] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def validate_workflow_result(self) -> "MinimalPlanningResult":
        if self.request_id != self.planner_context.request_id:
            raise ValueError("request_id must match planner_context.request_id")

        query_ids = [item.query_id for item in self.candidate_queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("candidate query ids must be unique")
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate ids must be unique")

        constraints = {
            item.constraint_id: item
            for item in (
                *self.planner_context.confirmed_hard_constraints,
                *self.planner_context.confirmed_soft_constraints,
            )
        }
        expected_adcode = self.planner_context.destination.administrative_code
        for query in self.candidate_queries:
            constraint = constraints.get(query.source_constraint_id)
            if constraint is None or constraint.kind != ConstraintKind.MUST_VISIT:
                raise ValueError(
                    "candidate queries must reference confirmed must_visit constraints"
                )
            if not isinstance(constraint.value, str) or query.requested_value != constraint.value:
                raise ValueError(
                    "candidate query requested_value must preserve the constraint value"
                )
            if expected_adcode is None or query.city_adcode != expected_adcode:
                raise ValueError("candidate query city must match the planner destination")

        expected_city = self.planner_context.destination.normalized_name
        if any(candidate.city != expected_city for candidate in self.candidates):
            raise ValueError("candidate city must match the normalized planner destination")
        if any(candidate.source.data_mode != self.data_mode for candidate in self.candidates):
            raise ValueError("candidate source mode must match the workflow data_mode")

        node_sequence = tuple(event.node for event in self.events)
        expected_prefix = (
            PlanningNodeName.COMPILE_CONTEXT,
            PlanningNodeName.CLARIFICATION_GATE,
        )
        if node_sequence[:2] != expected_prefix:
            raise ValueError(
                "workflow events must start with compile_context and clarification_gate"
            )
        if self.events[0].outcome != PlanningNodeOutcome.COMPILED:
            raise ValueError("compile_context must record a compiled outcome")

        search_is_ready = (
            PlannerCapability.CANDIDATE_SEARCH in self.planner_context.ready_capabilities
        )
        if self.status == PlanningWorkflowStatus.NEEDS_CLARIFICATION:
            if search_is_ready:
                raise ValueError("needs_clarification requires candidate search to be blocked")
            if len(self.events) != 2 or self.events[1].outcome != PlanningNodeOutcome.BLOCKED:
                raise ValueError("blocked workflow must stop at clarification_gate")
            if self.candidate_queries or self.candidates or self.provider_failures:
                raise ValueError("blocked workflow cannot contain provider search outputs")
            return self

        if not search_is_ready:
            raise ValueError("candidate workflow cannot run while candidate search is blocked")
        if len(self.events) != 3 or self.events[1].outcome != PlanningNodeOutcome.ALLOWED:
            raise ValueError("candidate workflow must pass clarification_gate")
        if self.events[2].node != PlanningNodeName.CANDIDATE_SEARCH:
            raise ValueError("third workflow event must be candidate_search")

        if self.status == PlanningWorkflowStatus.NO_CANDIDATE_QUERY:
            if self.events[2].outcome != PlanningNodeOutcome.SKIPPED:
                raise ValueError("no_candidate_query must record a skipped search")
            if self.candidate_queries or self.candidates or self.provider_failures:
                raise ValueError("skipped candidate search cannot contain provider outputs")
        elif self.status == PlanningWorkflowStatus.CANDIDATES_READY:
            if self.events[2].outcome != PlanningNodeOutcome.SUCCEEDED:
                raise ValueError("candidates_ready must record a succeeded search")
            if not self.candidate_queries or not self.candidates or self.provider_failures:
                raise ValueError(
                    "candidates_ready requires queries and candidates without failures"
                )
        elif self.status == PlanningWorkflowStatus.PROVIDER_FAILED:
            if self.events[2].outcome != PlanningNodeOutcome.FAILED:
                raise ValueError("provider_failed must record a failed search")
            if not self.candidate_queries or not self.provider_failures:
                raise ValueError("provider_failed requires queries and typed failures")
        return self
