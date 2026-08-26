from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText, Sha256Digest
from app.domain.money import BudgetCategory, MoneyRange


class BudgetEstimateStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class BudgetEstimateConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class BudgetEstimateMethod(StrEnum):
    CANDIDATE_PRICE_RANGE = "candidate_price_range"
    ITINERARY_PRICE_RANGE = "itinerary_price_range"
    ROUTE_REFERENCE = "route_reference"
    PLANNING_REFERENCE = "planning_reference"


class BudgetEstimateQuantityBasis(StrEnum):
    ROOM_NIGHT = "room_night"
    TRAVELER_DAY = "traveler_day"
    TRAVELER_ACTIVITY = "traveler_activity"
    TRAVELER_TRIP = "traveler_trip"
    TRAVELER_MEAL = "traveler_meal"
    TRAVELER_ROUTE_LEG = "traveler_route_leg"
    PARTY_TRIP = "party_trip"


class BudgetComparisonStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    WITHIN_BUDGET = "within_budget"
    POSSIBLE_OVERRUN = "possible_overrun"
    OVER_BUDGET = "over_budget"
    INCOMPLETE = "incomplete"


class BudgetAdviceCode(StrEnum):
    KEEP_BUFFER = "keep_buffer"
    LOWER_LODGING_TIER = "lower_lodging_tier"
    PRIORITIZE_FREE_ACTIVITIES = "prioritize_free_activities"
    USE_PUBLIC_TRANSPORT = "use_public_transport"


class BudgetEstimateExclusion(StrEnum):
    INTERCITY_TRANSPORT = "intercity_transport"
    SHOPPING = "shopping"
    BOOKING_FEES = "booking_fees"


class BudgetEstimateItem(DomainModel):
    category: BudgetCategory
    description: NonEmptyText
    quantity_basis: BudgetEstimateQuantityBasis
    quantity: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    unit_price: MoneyRange
    total: MoneyRange
    method: BudgetEstimateMethod
    confidence: BudgetEstimateConfidence
    basis_description: NonEmptyText

    @model_validator(mode="after")
    def validate_total(self) -> "BudgetEstimateItem":
        if (
            self.total.minimum != self.quantity * self.unit_price.minimum
            or self.total.maximum != self.quantity * self.unit_price.maximum
        ):
            raise ValueError("budget estimate item total must equal quantity times unit range")
        return self


class BudgetEstimate(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    estimator_version: Literal["budget-estimator-v1", "budget-estimator-v2"] = "budget-estimator-v2"
    assumption_version: Literal[
        "cn-independent-trip-reference-v1",
        "cn-itinerary-linked-reference-v2",
    ] = "cn-itinerary-linked-reference-v2"
    request_id: Identifier
    context_id: Identifier
    input_request_sha256: Sha256Digest
    status: BudgetEstimateStatus
    currency: Literal["CNY"] = "CNY"
    scope_categories: tuple[BudgetCategory, ...] = Field(min_length=1, max_length=6)
    items: tuple[BudgetEstimateItem, ...] = Field(max_length=6)
    unknown_categories: tuple[BudgetCategory, ...] = Field(max_length=6)
    total: MoneyRange | None = None
    per_traveler: MoneyRange | None = None
    per_day: MoneyRange | None = None
    budget_limit: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    comparison_status: BudgetComparisonStatus
    advice_codes: tuple[BudgetAdviceCode, ...] = Field(max_length=4)
    exclusions: tuple[BudgetEstimateExclusion, ...] = (
        BudgetEstimateExclusion.INTERCITY_TRANSPORT,
        BudgetEstimateExclusion.SHOPPING,
        BudgetEstimateExclusion.BOOKING_FEES,
    )

    @model_validator(mode="after")
    def validate_estimate(self) -> "BudgetEstimate":
        scope = set(self.scope_categories)
        item_categories = {item.category for item in self.items}
        unknown = set(self.unknown_categories)
        if len(scope) != len(self.scope_categories):
            raise ValueError("budget estimate scope categories must be unique")
        if len(item_categories) != len(self.items):
            raise ValueError("budget estimate item categories must be unique")
        if len(unknown) != len(self.unknown_categories):
            raise ValueError("budget estimate unknown categories must be unique")
        if item_categories & unknown or item_categories | unknown != scope:
            raise ValueError("estimated and unknown categories must partition the estimate scope")
        expected_order = tuple(category for category in BudgetCategory if category in scope)
        if self.scope_categories != expected_order:
            raise ValueError("budget estimate scope categories must use canonical order")
        if tuple(item.category for item in self.items) != tuple(
            category for category in expected_order if category in item_categories
        ):
            raise ValueError("budget estimate items must use canonical category order")
        if self.unknown_categories != tuple(
            category for category in expected_order if category in unknown
        ):
            raise ValueError("unknown budget categories must use canonical category order")

        if self.status == BudgetEstimateStatus.PARTIAL:
            if not self.unknown_categories:
                raise ValueError("partial budget estimates require unknown categories")
            if self.total is not None or self.per_traveler is not None or self.per_day is not None:
                raise ValueError("partial budget estimates cannot present an incomplete total")
            if self.comparison_status != BudgetComparisonStatus.INCOMPLETE:
                raise ValueError("partial budget estimates require an incomplete comparison")
            return self

        if self.unknown_categories:
            raise ValueError("complete budget estimates cannot contain unknown categories")
        if self.total is None or self.per_traveler is None or self.per_day is None:
            raise ValueError("complete budget estimates require total and normalized ranges")
        expected_minimum = sum((item.total.minimum for item in self.items), start=Decimal("0"))
        expected_maximum = sum((item.total.maximum for item in self.items), start=Decimal("0"))
        if self.total.minimum != expected_minimum or self.total.maximum != expected_maximum:
            raise ValueError("budget estimate total must equal category totals")
        if (
            self.budget_limit is None
            and self.comparison_status != BudgetComparisonStatus.NOT_REQUESTED
        ):
            raise ValueError("estimates without a budget require not_requested comparison")
        if self.budget_limit is not None and self.comparison_status in {
            BudgetComparisonStatus.NOT_REQUESTED,
            BudgetComparisonStatus.INCOMPLETE,
        }:
            raise ValueError("complete estimates with a budget require a budget comparison")
        return self
