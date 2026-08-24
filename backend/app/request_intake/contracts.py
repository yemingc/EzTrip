from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.agents.contracts import ConstraintDecision, ModelTokenUsage
from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.request import ConstraintSet, TripPace, TripRequest
from app.domain.sources import DataMode


class RequestEvidenceMode(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"


class RequestFieldName(StrEnum):
    ORIGIN_CITY = "origin_city"
    DESTINATION_CITY = "destination_city"
    START_DATE = "start_date"
    TRIP_DAYS = "trip_days"
    ADULTS = "adults"
    CHILDREN = "children"
    SENIORS = "seniors"
    BUDGET_LIMIT = "budget_limit"
    PACE = "pace"
    TRAVEL_STYLE = "travel_style"


class RequestFieldProposalItem(DomainModel):
    field: RequestFieldName
    value: str = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1, max_length=160)
    evidence_mode: RequestEvidenceMode


class RequestFieldProposalBatch(DomainModel):
    items: tuple[RequestFieldProposalItem, ...] = Field(default=(), max_length=16)


class RequestIntakeModelResponse(DomainModel):
    proposal: RequestFieldProposalBatch
    model: NonEmptyText
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None


class ProposedRequestFields(DomainModel):
    origin_city: str | None = None
    destination_city: str | None = None
    start_date: date | None = None
    trip_days: int | None = Field(default=None, ge=2, le=5)
    adults: int | None = Field(default=None, ge=0, le=20)
    children: int | None = Field(default=None, ge=0, le=20)
    seniors: int | None = Field(default=None, ge=0, le=20)
    budget_limit: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    pace: TripPace | None = None
    travel_styles: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_styles(self) -> "ProposedRequestFields":
        if len(self.travel_styles) != len(set(self.travel_styles)):
            raise ValueError("travel styles must be unique")
        return self


class RequestFieldDecisionStatus(StrEnum):
    MATCHED = "matched"
    CONFLICT = "conflict"
    PROPOSED = "proposed"
    UNMENTIONED = "unmentioned"
    NEEDS_CONFIRMATION = "needs_confirmation"


class RequestFieldDecision(DomainModel):
    field: RequestFieldName
    status: RequestFieldDecisionStatus
    form_value: str | None = None
    raw_proposed_value: str | None = None
    proposed_value: str | None = None
    evidence: str | None = None
    evidence_mode: RequestEvidenceMode | None = None
    message: NonEmptyText

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> "RequestFieldDecision":
        has_proposal = self.raw_proposed_value is not None
        if has_proposal != (self.evidence is not None and self.evidence_mode is not None):
            raise ValueError("proposed field decisions require evidence and evidence mode")
        if self.status == RequestFieldDecisionStatus.NEEDS_CONFIRMATION and (
            self.proposed_value is not None or not has_proposal
        ):
            raise ValueError("invalid proposals must preserve raw evidence without a value")
        if self.status == RequestFieldDecisionStatus.UNMENTIONED and has_proposal:
            raise ValueError("unmentioned fields cannot contain a proposal")
        return self


class RequestIntakeAgentResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    agent_version: Literal["request-intake-agent-v1"] = "request-intake-agent-v1"
    prompt_version: Literal["request-field-extraction-v1"] = "request-field-extraction-v1"
    request_id: Identifier
    raw_text_sha256: Sha256Digest
    reference_date: date
    model: NonEmptyText
    latency_ms: int = Field(ge=0)
    usage: ModelTokenUsage | None = None
    decisions: tuple[RequestFieldDecision, ...]
    proposed_fields: ProposedRequestFields
    clarifications: tuple[NonEmptyText, ...] = ()


class RequestIntakeFormValues(DomainModel):
    origin_city: str | None = None
    destination_city: NonEmptyText
    start_date: date
    trip_days: int = Field(ge=2, le=5)
    adults: int = Field(ge=0, le=20)
    children: int = Field(default=0, ge=0, le=20)
    seniors: int = Field(default=0, ge=0, le=20)
    rooms: int = Field(default=1, ge=1, le=20)
    budget_limit: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    pace: TripPace

    @model_validator(mode="after")
    def validate_party(self) -> "RequestIntakeFormValues":
        total = self.adults + self.children + self.seniors
        if total == 0:
            raise ValueError("party must contain at least one traveler")
        if self.adults + self.seniors == 0:
            raise ValueError("children require an adult or senior traveler")
        if self.rooms > total:
            raise ValueError("rooms cannot exceed total travelers")
        return self


class RequestIntakeCreateRequest(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    raw_text: NonEmptyText
    reference_date: date
    data_mode: Literal[DataMode.FIXTURE, DataMode.LIVE] = DataMode.FIXTURE
    form: RequestIntakeFormValues


class RequestConfirmationDraft(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    intake_version: Literal["request-to-plan-v1"] = "request-to-plan-v1"
    draft_id: Identifier
    data_mode: Literal[DataMode.FIXTURE, DataMode.LIVE]
    raw_text_sha256: Sha256Digest
    field_model: NonEmptyText
    constraint_model: NonEmptyText
    model_call_count: int = Field(ge=0, le=2)
    field_decisions: tuple[RequestFieldDecision, ...]
    proposed_fields: ProposedRequestFields
    constraint_decisions: tuple[ConstraintDecision, ...]
    proposed_constraints: ConstraintSet
    clarifications: tuple[NonEmptyText, ...] = ()
    proposal_can_confirm: bool


class RequestIntakeSelection(StrEnum):
    PROPOSAL = "proposal"
    FORM = "form"


class RequestIntakeConfirmRequest(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    selection: RequestIntakeSelection
    selected_destination_adcode: str | None = Field(default=None, pattern=r"^\d{6}$")


class ConfirmedRequestIntake(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    confirmation_id: Identifier
    draft_id: Identifier
    selection: RequestIntakeSelection
    data_mode: Literal[DataMode.FIXTURE, DataMode.LIVE]
    selected_destination_adcode: str | None = None
    request: TripRequest
