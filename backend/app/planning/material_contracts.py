from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, Sha256Digest
from app.domain.candidates import CandidatePOI, CandidateStay
from app.domain.context import PlannerContext
from app.domain.money import BudgetCategory
from app.domain.provider import ProviderErrorCategory
from app.domain.sources import DataMode
from app.domain.travel_data import RouteLeg, RouteMode
from app.planning.specialist_contracts import (
    SpecialistBranchStatus,
    SpecialistFanoutResult,
    SpecialistFanoutStatus,
    SpecialistName,
)


class PlanningCandidateKind(StrEnum):
    POI = "poi"
    STAY = "stay"


class RouteEdgeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RouteFailureCategory(StrEnum):
    PROVIDER = "provider"
    DEPENDENCY = "dependency"


class RouteMatrixStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    NOT_REQUIRED = "not_required"


class RouteMatrixReason(StrEnum):
    CAPABILITY_BLOCKED = "capability_blocked"
    NO_EXPLORE_CANDIDATES = "no_explore_candidates"
    INSUFFICIENT_CANDIDATE_PAIR = "insufficient_candidate_pair"


class BudgetAllocationStatus(StrEnum):
    ALLOCATED = "allocated"
    BLOCKED = "blocked"
    NOT_REQUESTED = "not_requested"


class BudgetAllocationReason(StrEnum):
    MISSING_BUDGET = "missing_budget"
    MISSING_ROOMS = "missing_rooms"


class BudgetQuantityBasis(StrEnum):
    ROOM_NIGHT = "room_night"
    PARTY_DAY = "party_day"
    TRAVELER_TRIP = "traveler_trip"
    PARTY_TRIP = "party_trip"


class PlanningMaterialStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class PlanningMaterialIssueCode(StrEnum):
    SPECIALIST_INCOMPLETE = "specialist_incomplete"
    ROUTE_MATRIX_INCOMPLETE = "route_matrix_incomplete"
    BUDGET_NOT_ALLOCATED = "budget_not_allocated"
    STAY_ANCHOR_MISSING = "stay_anchor_missing"


class PlanningShortlist(DomainModel):
    policy_version: Literal["planning-shortlist-v1"] = "planning-shortlist-v1"
    poi_candidates: tuple[CandidatePOI, ...] = Field(max_length=4)
    primary_stay: CandidateStay | None = None
    omitted_poi_ids: tuple[Identifier, ...] = ()
    omitted_stay_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_shortlist(self) -> "PlanningShortlist":
        selected_ids = [item.candidate_id for item in self.poi_candidates]
        if self.primary_stay is not None:
            selected_ids.append(self.primary_stay.candidate_id)
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("planning shortlist candidate ids must be unique")
        omitted_ids = (*self.omitted_poi_ids, *self.omitted_stay_ids)
        if len(omitted_ids) != len(set(omitted_ids)):
            raise ValueError("omitted planning candidate ids must be unique")
        if set(selected_ids) & set(omitted_ids):
            raise ValueError("selected and omitted planning candidate ids cannot overlap")
        return self


class RouteEdgeFailure(DomainModel):
    category: RouteFailureCategory
    error_code: Identifier
    retryable: bool
    provider_category: ProviderErrorCategory | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> "RouteEdgeFailure":
        if (self.category == RouteFailureCategory.PROVIDER) != (self.provider_category is not None):
            raise ValueError("only Provider route failures carry a Provider category")
        if self.category != RouteFailureCategory.PROVIDER and self.retryable:
            raise ValueError("only typed Provider route failures may be retryable")
        return self


class RouteMatrixEdge(DomainModel):
    edge_id: Identifier
    origin_candidate_id: Identifier
    origin_kind: PlanningCandidateKind
    destination_candidate_id: Identifier
    destination_kind: PlanningCandidateKind
    status: RouteEdgeStatus
    route: RouteLeg | None = None
    failure: RouteEdgeFailure | None = None

    @model_validator(mode="after")
    def validate_edge(self) -> "RouteMatrixEdge":
        if self.origin_candidate_id == self.destination_candidate_id:
            raise ValueError("route matrix edges cannot be self-referential")
        if self.status == RouteEdgeStatus.SUCCEEDED:
            if self.route is None or self.failure is not None:
                raise ValueError("successful route edges require only a RouteLeg")
            if (
                self.route.origin.candidate_id != self.origin_candidate_id
                or self.route.destination.candidate_id != self.destination_candidate_id
            ):
                raise ValueError("RouteLeg endpoints must match route matrix candidate ids")
        elif self.failure is None or self.route is not None:
            raise ValueError("failed route edges require only typed failure data")
        return self


class RouteMatrix(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    matrix_version: Literal["route-matrix-v1"] = "route-matrix-v1"
    request_id: Identifier
    context_id: Identifier
    data_mode: Literal[DataMode.LIVE, DataMode.FIXTURE]
    status: RouteMatrixStatus
    reason: RouteMatrixReason | None = None
    route_mode: Literal[RouteMode.TRANSIT] = RouteMode.TRANSIT
    poi_candidate_ids: tuple[Identifier, ...] = Field(max_length=4)
    primary_stay_id: Identifier | None = None
    edges: tuple[RouteMatrixEdge, ...] = Field(max_length=20)
    expected_edge_count: int = Field(ge=0, le=20)
    succeeded_edge_count: int = Field(ge=0, le=20)
    failed_edge_count: int = Field(ge=0, le=20)
    provider_call_count: int = Field(ge=0, le=20)
    max_concurrency: int = Field(ge=1, le=8)
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_matrix(self) -> "RouteMatrix":
        if len(self.poi_candidate_ids) != len(set(self.poi_candidate_ids)):
            raise ValueError("route matrix POI ids must be unique")
        if self.primary_stay_id in set(self.poi_candidate_ids):
            raise ValueError("route matrix stay id cannot duplicate a POI id")
        expected_pairs = tuple(
            (origin, destination)
            for origin in self.poi_candidate_ids
            for destination in self.poi_candidate_ids
            if origin != destination
        )
        if self.primary_stay_id is not None:
            expected_pairs = (
                *expected_pairs,
                *(
                    pair
                    for poi_id in self.poi_candidate_ids
                    for pair in (
                        (self.primary_stay_id, poi_id),
                        (poi_id, self.primary_stay_id),
                    )
                ),
            )
        if self.status == RouteMatrixStatus.BLOCKED:
            if (
                self.reason != RouteMatrixReason.CAPABILITY_BLOCKED
                or self.edges
                or any(
                    (
                        self.expected_edge_count,
                        self.succeeded_edge_count,
                        self.failed_edge_count,
                        self.provider_call_count,
                    )
                )
            ):
                raise ValueError("blocked route matrix requires only its capability reason")
            return self
        actual_pairs = tuple(
            (item.origin_candidate_id, item.destination_candidate_id) for item in self.edges
        )
        if actual_pairs != expected_pairs:
            raise ValueError("route matrix edges must exactly cover the ordered candidate pairs")
        if len({item.edge_id for item in self.edges}) != len(self.edges):
            raise ValueError("route matrix edge ids must be unique")
        succeeded = sum(item.status == RouteEdgeStatus.SUCCEEDED for item in self.edges)
        failed = sum(item.status == RouteEdgeStatus.FAILED for item in self.edges)
        if (
            self.expected_edge_count != len(expected_pairs)
            or self.succeeded_edge_count != succeeded
            or self.failed_edge_count != failed
            or self.provider_call_count != len(self.edges)
        ):
            raise ValueError("route matrix counts must match its edges")
        if any(
            item.route is not None
            and (
                item.route.mode != self.route_mode or item.route.source.data_mode != self.data_mode
            )
            for item in self.edges
        ):
            raise ValueError("route matrix RouteLeg values must match mode and data mode")
        expected_status = RouteMatrixStatus.COMPLETE
        expected_reason = None
        if not self.poi_candidate_ids:
            expected_status = RouteMatrixStatus.UNAVAILABLE
            expected_reason = RouteMatrixReason.NO_EXPLORE_CANDIDATES
        elif not expected_pairs:
            expected_status = RouteMatrixStatus.NOT_REQUIRED
            expected_reason = RouteMatrixReason.INSUFFICIENT_CANDIDATE_PAIR
        elif failed == len(expected_pairs):
            expected_status = RouteMatrixStatus.FAILED
        elif failed:
            expected_status = RouteMatrixStatus.PARTIAL
        if self.status != expected_status or self.reason != expected_reason:
            raise ValueError("route matrix status and reason must match edge outcomes")
        return self


class BudgetAllocationItem(DomainModel):
    category: BudgetCategory
    policy_weight: Decimal = Field(gt=0, le=1, max_digits=6, decimal_places=4)
    target_amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    quantity_basis: BudgetQuantityBasis
    reference_quantity: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    target_per_unit: Decimal = Field(ge=0, max_digits=12, decimal_places=2)

    @model_validator(mode="after")
    def validate_quantity_basis(self) -> "BudgetAllocationItem":
        expected_basis = {
            BudgetCategory.LODGING: BudgetQuantityBasis.ROOM_NIGHT,
            BudgetCategory.TRANSPORT: BudgetQuantityBasis.PARTY_DAY,
            BudgetCategory.FOOD: BudgetQuantityBasis.PARTY_DAY,
            BudgetCategory.ADMISSION: BudgetQuantityBasis.TRAVELER_TRIP,
            BudgetCategory.ACTIVITY: BudgetQuantityBasis.TRAVELER_TRIP,
            BudgetCategory.OTHER: BudgetQuantityBasis.PARTY_TRIP,
        }[self.category]
        if self.quantity_basis != expected_basis:
            raise ValueError("budget allocation quantity basis must match its category")
        return self


class BudgetAllocation(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    allocator_version: Literal["budget-allocator-v1"] = "budget-allocator-v1"
    policy_version: Literal["cn-city-trip-weights-v1"] = "cn-city-trip-weights-v1"
    request_id: Identifier
    context_id: Identifier
    input_request_sha256: Sha256Digest
    status: BudgetAllocationStatus
    reason: BudgetAllocationReason | None = None
    total_limit: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: Literal["CNY"] = "CNY"
    hard_limit: bool | None = None
    included_categories: tuple[BudgetCategory, ...] = ()
    excluded_categories: tuple[BudgetCategory, ...] = ()
    allocations: tuple[BudgetAllocationItem, ...] = Field(default=(), max_length=6)

    @model_validator(mode="after")
    def validate_allocation(self) -> "BudgetAllocation":
        if self.status != BudgetAllocationStatus.ALLOCATED:
            if (
                self.reason is None
                or self.total_limit is not None
                or self.hard_limit is not None
                or self.included_categories
                or self.excluded_categories
                or self.allocations
            ):
                raise ValueError("unallocated budget results carry only a typed reason")
            return self
        if self.reason is not None or self.total_limit is None or self.hard_limit is None:
            raise ValueError("allocated budget results require budget context without a reason")
        included = set(self.included_categories)
        excluded = set(self.excluded_categories)
        if included & excluded or included | excluded != set(BudgetCategory):
            raise ValueError("budget allocation categories must partition every category")
        expected_order = tuple(category for category in BudgetCategory if category in included)
        if tuple(item.category for item in self.allocations) != expected_order:
            raise ValueError("budget allocation items must match included categories in order")
        assert self.total_limit is not None
        if sum((item.target_amount for item in self.allocations), start=Decimal("0")) != (
            self.total_limit
        ):
            raise ValueError("budget target amounts must sum exactly to the total limit")
        return self


class PlanningMaterialBundle(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    bundle_version: Literal["planning-materials-v1"] = "planning-materials-v1"
    request_id: Identifier
    context_id: Identifier
    data_mode: Literal[DataMode.LIVE, DataMode.FIXTURE]
    status: PlanningMaterialStatus
    issues: tuple[PlanningMaterialIssueCode, ...]
    planner_context: PlannerContext
    specialist_result: SpecialistFanoutResult
    shortlist: PlanningShortlist
    route_matrix: RouteMatrix
    budget_allocation: BudgetAllocation

    @model_validator(mode="after")
    def validate_bundle(self) -> "PlanningMaterialBundle":
        identity_values = {
            (self.request_id, self.context_id),
            (self.planner_context.request_id, self.planner_context.context_id),
            (self.specialist_result.request_id, self.specialist_result.context_id),
            (self.route_matrix.request_id, self.route_matrix.context_id),
            (self.budget_allocation.request_id, self.budget_allocation.context_id),
        }
        if len(identity_values) != 1:
            raise ValueError("planning material components must preserve request/context identity")
        if (
            self.data_mode != self.specialist_result.data_mode
            or self.data_mode != self.route_matrix.data_mode
        ):
            raise ValueError("planning material components must preserve data mode")
        explore_branch = next(
            item
            for item in self.specialist_result.branches
            if item.specialist == SpecialistName.EXPLORE
        )
        available_pois = (
            tuple(
                item.candidate
                for item in sorted(
                    explore_branch.explore_result.recommendations,
                    key=lambda item: item.proposal.rank,
                )
            )
            if explore_branch.explore_result is not None
            else ()
        )
        stay_branch = next(
            item
            for item in self.specialist_result.branches
            if item.specialist == SpecialistName.STAY
        )
        available_stays = (
            tuple(
                item.candidate
                for item in sorted(
                    stay_branch.stay_result.recommendations,
                    key=lambda item: item.proposal.rank,
                )
            )
            if stay_branch.stay_result is not None
            else ()
        )
        if self.shortlist.poi_candidates != available_pois[:4]:
            raise ValueError("planning shortlist must take the four highest-ranked POIs")
        if self.shortlist.omitted_poi_ids != tuple(
            item.candidate_id for item in available_pois[4:]
        ):
            raise ValueError("planning shortlist must expose omitted POI ids")
        expected_stay = available_stays[0] if available_stays else None
        if self.shortlist.primary_stay != expected_stay:
            raise ValueError("planning shortlist must use the highest-ranked stay as its anchor")
        if self.shortlist.omitted_stay_ids != tuple(
            item.candidate_id for item in available_stays[1:]
        ):
            raise ValueError("planning shortlist must expose omitted stay ids")
        if self.route_matrix.poi_candidate_ids != tuple(
            item.candidate_id for item in self.shortlist.poi_candidates
        ) or self.route_matrix.primary_stay_id != (
            self.shortlist.primary_stay.candidate_id
            if self.shortlist.primary_stay is not None
            else None
        ):
            raise ValueError("route matrix scope must match the planning shortlist")
        expected_issues: list[PlanningMaterialIssueCode] = []
        if self.specialist_result.status != SpecialistFanoutStatus.COMPLETE:
            expected_issues.append(PlanningMaterialIssueCode.SPECIALIST_INCOMPLETE)
        if self.route_matrix.status not in {
            RouteMatrixStatus.COMPLETE,
            RouteMatrixStatus.NOT_REQUIRED,
        }:
            expected_issues.append(PlanningMaterialIssueCode.ROUTE_MATRIX_INCOMPLETE)
        if self.budget_allocation.status != BudgetAllocationStatus.ALLOCATED:
            expected_issues.append(PlanningMaterialIssueCode.BUDGET_NOT_ALLOCATED)
        if self.shortlist.primary_stay is None:
            expected_issues.append(PlanningMaterialIssueCode.STAY_ANCHOR_MISSING)
        if self.issues != tuple(expected_issues):
            raise ValueError("planning material issues must match component outcomes")
        expected_status = PlanningMaterialStatus.READY
        if (
            self.specialist_result.status == SpecialistFanoutStatus.BLOCKED
            or self.route_matrix.status
            in {RouteMatrixStatus.BLOCKED, RouteMatrixStatus.UNAVAILABLE}
            or explore_branch.status != SpecialistBranchStatus.SUCCEEDED
        ):
            expected_status = PlanningMaterialStatus.BLOCKED
        elif expected_issues:
            expected_status = PlanningMaterialStatus.PARTIAL
        if self.status != expected_status:
            raise ValueError("planning material status must match component outcomes")
        return self
