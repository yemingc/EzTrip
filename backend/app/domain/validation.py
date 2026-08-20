from enum import StrEnum

from pydantic import model_validator

from app.domain.base import DomainModel, Identifier, NonEmptyText


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
