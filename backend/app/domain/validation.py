from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText
from app.domain.money import BudgetCategory


class IssueSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


class ResponsibleNode(StrEnum):
    CONSTRAINT = "constraint"
    EXPLORE = "explore"
    STAY = "stay"
    WEATHER = "weather"
    ROUTE = "route"
    PLAN = "plan"
    BUDGET = "budget"
    VALIDATOR = "validator"


class RepairAction(StrEnum):
    NONE = "none"
    RERUN_CONSTRAINT = "rerun_constraint"
    RERUN_EXPLORE = "rerun_explore"
    RERUN_STAY = "rerun_stay"
    RERUN_ROUTE = "rerun_route"
    REPLAN_DAY = "replan_day"
    RECALCULATE_BUDGET = "recalculate_budget"
    ASK_USER = "ask_user"


class ValidationEvidence(DomainModel):
    field_path: NonEmptyText
    description: NonEmptyText
    observed_value: str | int | float | bool | None = None


class ValidationIssue(DomainModel):
    issue_id: Identifier
    rule_code: NonEmptyText
    severity: IssueSeverity
    message: NonEmptyText
    evidence: tuple[ValidationEvidence, ...]
    responsible_node: ResponsibleNode
    repairable: bool
    repair_action: RepairAction
    requires_user_confirmation: bool = False

    @model_validator(mode="after")
    def validate_repair_contract(self) -> "ValidationIssue":
        if self.repairable and self.repair_action == RepairAction.NONE:
            raise ValueError("repairable issues require a repair action")
        if not self.repairable and self.repair_action != RepairAction.NONE:
            raise ValueError("non-repairable issues must use repair_action=none")
        if self.repair_action == RepairAction.ASK_USER and not self.requires_user_confirmation:
            raise ValueError("ask_user repair requires user confirmation")
        return self


class BudgetAssessmentStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    WITHIN_LIMIT = "within_limit"
    POSSIBLE_OVERRUN = "possible_overrun"
    EXCEEDED = "exceeded"
    INCOMPLETE = "incomplete"


class BudgetValidationSummary(DomainModel):
    status: BudgetAssessmentStatus
    currency: Literal["CNY"] = "CNY"
    total_limit: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    included_categories: tuple[BudgetCategory, ...] = ()
    missing_categories: tuple[BudgetCategory, ...] = ()
    considered_cost_item_ids: tuple[Identifier, ...] = ()
    excluded_cost_item_ids: tuple[Identifier, ...] = ()
    total_minimum: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    total_maximum: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    minimum_gap: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    maximum_gap: Decimal = Field(ge=0, max_digits=12, decimal_places=2)

    @model_validator(mode="after")
    def validate_budget_summary(self) -> "BudgetValidationSummary":
        if self.total_maximum < self.total_minimum:
            raise ValueError("budget total maximum cannot be below minimum")
        all_cost_ids = (*self.considered_cost_item_ids, *self.excluded_cost_item_ids)
        if len(all_cost_ids) != len(set(all_cost_ids)):
            raise ValueError("budget cost item partitions must be unique")
        if len(self.included_categories) != len(set(self.included_categories)):
            raise ValueError("included budget categories must be unique")
        if len(self.missing_categories) != len(set(self.missing_categories)):
            raise ValueError("missing budget categories must be unique")
        if not set(self.missing_categories) <= set(self.included_categories):
            raise ValueError("missing categories must be included in the budget scope")
        if self.status == BudgetAssessmentStatus.NOT_REQUESTED:
            if self.total_limit is not None or self.included_categories or self.missing_categories:
                raise ValueError("not_requested budget cannot carry a limit or scope")
            if self.considered_cost_item_ids or any(
                value != 0
                for value in (
                    self.total_minimum,
                    self.total_maximum,
                    self.minimum_gap,
                    self.maximum_gap,
                )
            ):
                raise ValueError("not_requested budget cannot claim assessed totals")
        elif self.total_limit is None or not self.included_categories:
            raise ValueError("assessed budget requires a limit and included categories")
        else:
            expected_minimum_gap = max(self.total_minimum - self.total_limit, Decimal("0"))
            expected_maximum_gap = max(self.total_maximum - self.total_limit, Decimal("0"))
            if self.minimum_gap != expected_minimum_gap or self.maximum_gap != expected_maximum_gap:
                raise ValueError("budget gaps must be recomputed from totals and limit")
            expected_status = BudgetAssessmentStatus.WITHIN_LIMIT
            if expected_minimum_gap > 0:
                expected_status = BudgetAssessmentStatus.EXCEEDED
            elif self.missing_categories:
                expected_status = BudgetAssessmentStatus.INCOMPLETE
            elif expected_maximum_gap > 0:
                expected_status = BudgetAssessmentStatus.POSSIBLE_OVERRUN
            if self.status != expected_status:
                raise ValueError("budget status must match totals, limit, and category coverage")
        return self


class PlanValidationStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    CONFLICTED = "conflicted"


class PlanValidationReport(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    validator_version: Literal["deterministic-plan-validator-v1"] = (
        "deterministic-plan-validator-v1"
    )
    request_id: Identifier
    plan_id: Identifier
    status: PlanValidationStatus
    can_finalize: bool
    budget: BudgetValidationSummary
    issues: tuple[ValidationIssue, ...] = ()
    passed_rule_codes: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_report_status(self) -> "PlanValidationReport":
        issue_ids = [item.issue_id for item in self.issues]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("validation issue ids must be unique")
        rule_codes = [item.rule_code for item in self.issues]
        if len(rule_codes) != len(set(rule_codes)):
            raise ValueError("validation issue rule codes must be unique")
        if len(self.passed_rule_codes) != len(set(self.passed_rule_codes)):
            raise ValueError("passed rule codes must be unique")
        if set(rule_codes) & set(self.passed_rule_codes):
            raise ValueError("a validation rule cannot both pass and fail")
        has_error = any(item.severity == IssueSeverity.ERROR for item in self.issues)
        has_warning = any(item.severity == IssueSeverity.WARNING for item in self.issues)
        expected_status = PlanValidationStatus.PASSED
        if has_error:
            expected_status = PlanValidationStatus.CONFLICTED
        elif has_warning:
            expected_status = PlanValidationStatus.WARNING
        if self.status != expected_status:
            raise ValueError("validation status must match issue severities")
        if self.can_finalize == has_error:
            raise ValueError("can_finalize must be false exactly when errors exist")
        return self
