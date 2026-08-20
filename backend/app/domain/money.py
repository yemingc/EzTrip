from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.sources import DataMode, SourceReference


class BudgetCategory(StrEnum):
    LODGING = "lodging"
    TRANSPORT = "transport"
    FOOD = "food"
    ADMISSION = "admission"
    ACTIVITY = "activity"
    OTHER = "other"


class MoneyRange(DomainModel):
    minimum: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    maximum: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    currency: Literal["CNY"] = "CNY"

    @model_validator(mode="after")
    def validate_range(self) -> "MoneyRange":
        if self.maximum < self.minimum:
            raise ValueError("maximum must be greater than or equal to minimum")
        return self


class CostItem(DomainModel):
    cost_item_id: Identifier
    category: BudgetCategory
    description: NonEmptyText
    quantity: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    unit_price: MoneyRange
    source: SourceReference
    is_estimate: bool

    @model_validator(mode="after")
    def validate_estimate_semantics(self) -> "CostItem":
        is_range = self.unit_price.minimum != self.unit_price.maximum
        if (self.source.data_mode == DataMode.ESTIMATE or is_range) and not self.is_estimate:
            raise ValueError("estimated or ranged prices must set is_estimate=true")
        return self

    @property
    def total_minimum(self) -> Decimal:
        return self.quantity * self.unit_price.minimum

    @property
    def total_maximum(self) -> Decimal:
        return self.quantity * self.unit_price.maximum
