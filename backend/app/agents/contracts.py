from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
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
