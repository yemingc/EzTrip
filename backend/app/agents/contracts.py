from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.candidates import CandidatePOI
from app.domain.planning import DayPlan, ItineraryItem
from app.domain.request import Constraint, ConstraintKind, ConstraintSet, ConstraintStrength


class ConstraintEvidenceMode(StrEnum):
    """Whether the quoted evidence is a direct requirement or an uncertain suggestion."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class ConstraintProposalItem(DomainModel):
    kind: ConstraintKind
    value: str = Field(min_length=1, max_length=100)
    strength: ConstraintStrength
    priority: int = Field(ge=1, le=5)
    evidence: str = Field(min_length=1, max_length=160)
    evidence_mode: ConstraintEvidenceMode


class ConstraintProposalBatch(DomainModel):
    items: tuple[ConstraintProposalItem, ...] = Field(default=(), max_length=12)


class ModelTokenUsage(DomainModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> "ModelTokenUsage":
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens must equal prompt_tokens + completion_tokens")
        return self


class ConstraintModelResponse(DomainModel):
    proposal: ConstraintProposalBatch
    model: NonEmptyText
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None


class ConstraintDecision(DomainModel):
    constraint: Constraint
    evidence: str = Field(min_length=1, max_length=160)
    evidence_mode: ConstraintEvidenceMode


class ConstraintAgentResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    agent_version: Literal["constraint-agent-v1"] = "constraint-agent-v1"
    prompt_version: Literal["constraint-extraction-v1"] = "constraint-extraction-v1"
    request_id: Identifier
    raw_text_sha256: Sha256Digest
    model: NonEmptyText
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None
    decisions: tuple[ConstraintDecision, ...] = ()
    constraints: ConstraintSet
    hitl_constraint_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_decision_boundary(self) -> "ConstraintAgentResult":
        decision_constraints = tuple(item.constraint for item in self.decisions)
        if decision_constraints != self.constraints.items:
            raise ValueError("decisions and constraints must preserve the same order and values")

        expected_hitl: list[str] = []
        for decision in self.decisions:
            constraint = decision.constraint
            if decision.evidence_mode == ConstraintEvidenceMode.EXPLICIT:
                if not constraint.confirmed or constraint.source.value != "user_explicit":
                    raise ValueError("explicit evidence must map to a confirmed user constraint")
            else:
                if constraint.confirmed or constraint.source.value != "agent_inferred":
                    raise ValueError("inferred evidence must remain an unconfirmed agent proposal")
                expected_hitl.append(constraint.constraint_id)
        if self.hitl_constraint_ids != tuple(expected_hitl):
            raise ValueError("hitl_constraint_ids must contain every inferred proposal")
        return self


class PlannerPlacementProposal(DomainModel):
    candidate_id: Identifier
    day_number: int = Field(ge=1, le=5)
    start_time: str = Field(pattern=r"^(?:0[8-9]|1\d|2[0-1]):(?:00|30)$")
    reason: str = Field(min_length=1, max_length=160)


class PlannerProposalBatch(DomainModel):
    items: tuple[PlannerPlacementProposal, ...] = Field(min_length=1, max_length=12)


class PlannerModelResponse(DomainModel):
    proposal: PlannerProposalBatch
    model: NonEmptyText
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None


class PlannerPlacementDecision(DomainModel):
    proposal: PlannerPlacementProposal
    item: ItineraryItem

    @model_validator(mode="after")
    def validate_candidate_identity(self) -> "PlannerPlacementDecision":
        if self.item.candidate_id != self.proposal.candidate_id:
            raise ValueError("planner decision must preserve the proposed candidate_id")
        return self


class SinglePlannerAgentResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    agent_version: Literal["single-planner-v1"] = "single-planner-v1"
    prompt_version: Literal["candidate-placement-v1"] = "candidate-placement-v1"
    request_id: Identifier
    context_id: Identifier
    input_candidates_sha256: Sha256Digest
    model: NonEmptyText
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None
    decisions: tuple[PlannerPlacementDecision, ...] = Field(min_length=1)
    day_plans: tuple[DayPlan, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_partial_day_plans(self) -> "SinglePlannerAgentResult":
        decision_ids = [item.proposal.candidate_id for item in self.decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("planner decisions must not repeat candidate ids")
        flattened = tuple(item for day in self.day_plans for item in day.items)
        if flattened != tuple(item.item for item in self.decisions):
            raise ValueError("day_plans must preserve every planner decision in timeline order")
        dates = [day.date for day in self.day_plans]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ValueError("partial day plans must have unique sorted dates")
        return self


class ExploreQueryKind(StrEnum):
    ATTRACTION = "attraction"
    DINING = "dining"


class ExploreEvidenceKind(StrEnum):
    QUERY_MATCH = "query_match"
    CATEGORY = "category"
    DISTRICT = "district"
    ENVIRONMENT = "environment"
    TAG = "tag"


class ExploreQueryProposal(DomainModel):
    kind: ExploreQueryKind
    keywords: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=160)
    context_refs: tuple[NonEmptyText, ...] = Field(default=(), max_length=4)


class ExploreQueryProposalBatch(DomainModel):
    items: tuple[ExploreQueryProposal, ...] = Field(min_length=1, max_length=4)


class ExploreQueryModelResponse(DomainModel):
    proposal: ExploreQueryProposalBatch
    model: NonEmptyText
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None


class ExploreSearchQuery(DomainModel):
    query_id: Identifier
    kind: ExploreQueryKind
    keywords: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=160)
    context_refs: tuple[NonEmptyText, ...] = Field(default=(), max_length=4)


class ExploreCandidateObservation(DomainModel):
    candidate: CandidatePOI
    query_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_unique_query_ids(self) -> "ExploreCandidateObservation":
        if len(self.query_ids) != len(set(self.query_ids)):
            raise ValueError("candidate observation query ids must be unique")
        return self


class ExploreEvidenceReference(DomainModel):
    kind: ExploreEvidenceKind
    value: NonEmptyText


class ExploreCandidateSelectionProposal(DomainModel):
    candidate_id: Identifier
    rank: int = Field(ge=1, le=6)
    reason: str = Field(min_length=1, max_length=160)
    evidence: tuple[ExploreEvidenceReference, ...] = Field(min_length=1, max_length=4)


class ExploreSelectionProposalBatch(DomainModel):
    items: tuple[ExploreCandidateSelectionProposal, ...] = Field(min_length=1, max_length=6)


class ExploreSelectionModelResponse(DomainModel):
    proposal: ExploreSelectionProposalBatch
    model: NonEmptyText
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None


class ExploreRecommendation(DomainModel):
    proposal: ExploreCandidateSelectionProposal
    candidate: CandidatePOI
    query_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_candidate_identity(self) -> "ExploreRecommendation":
        if self.candidate.candidate_id != self.proposal.candidate_id:
            raise ValueError("Explore recommendation must preserve the proposed candidate_id")
        return self


class ExploreAgentResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    agent_version: Literal["explore-agent-v1"] = "explore-agent-v1"
    query_prompt_version: Literal["explore-query-strategy-v1"] = "explore-query-strategy-v1"
    selection_prompt_version: Literal["explore-candidate-selection-v1"] = (
        "explore-candidate-selection-v1"
    )
    request_id: Identifier
    context_id: Identifier
    candidate_set_sha256: Sha256Digest
    queries: tuple[ExploreSearchQuery, ...] = Field(min_length=1, max_length=4)
    observations: tuple[ExploreCandidateObservation, ...] = Field(min_length=1, max_length=12)
    recommendations: tuple[ExploreRecommendation, ...] = Field(min_length=1, max_length=6)
    query_model: NonEmptyText
    selection_model: NonEmptyText
    query_latency_ms: int = Field(ge=0)
    selection_latency_ms: int = Field(ge=0)
    query_usage: ModelTokenUsage | None = None
    selection_usage: ModelTokenUsage | None = None

    @model_validator(mode="after")
    def validate_grounded_recommendations(self) -> "ExploreAgentResult":
        query_ids = [item.query_id for item in self.queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("Explore queries must have unique ids")
        observed_ids = [item.candidate.candidate_id for item in self.observations]
        if len(observed_ids) != len(set(observed_ids)):
            raise ValueError("Explore observations must have unique candidate ids")
        known_query_ids = set(query_ids)
        if any(not set(item.query_ids).issubset(known_query_ids) for item in self.observations):
            raise ValueError("Explore observations must reference known query ids")
        observations_by_id = {item.candidate.candidate_id: item for item in self.observations}
        recommendation_ids = [item.candidate.candidate_id for item in self.recommendations]
        if len(recommendation_ids) != len(set(recommendation_ids)):
            raise ValueError("Explore recommendations must have unique candidate ids")
        if not set(recommendation_ids).issubset(observed_ids):
            raise ValueError("Explore recommendations must come from observations")
        if any(
            item.candidate != observations_by_id[item.candidate.candidate_id].candidate
            or item.query_ids != observations_by_id[item.candidate.candidate_id].query_ids
            for item in self.recommendations
        ):
            raise ValueError("Explore recommendations must preserve observed facts and lineage")
        ranks = [item.proposal.rank for item in self.recommendations]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("Explore recommendation ranks must be contiguous and ordered")
        return self
