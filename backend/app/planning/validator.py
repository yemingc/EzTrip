import hashlib
from collections import Counter
from decimal import Decimal

from app.domain.planning import ActivityKind, PlanStatus, TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.validation import (
    BudgetAssessmentStatus,
    BudgetValidationSummary,
    IssueSeverity,
    PlanValidationReport,
    PlanValidationStatus,
    RepairAction,
    ResponsibleNode,
    ValidationEvidence,
    ValidationIssue,
)

VALIDATOR_VERSION = "deterministic-plan-validator-v1"
ZERO = Decimal("0.00")


def _issue_id(plan_id: str, rule_code: str) -> str:
    digest = hashlib.sha256(f"{plan_id}|{rule_code}".encode()).hexdigest()[:12]
    slug = rule_code.replace(".", "-").replace("_", "-")
    return f"issue-{slug}-{digest}"


def _issue(
    plan: TripPlan,
    *,
    rule_code: str,
    severity: IssueSeverity,
    message: str,
    evidence: tuple[ValidationEvidence, ...],
    responsible_node: ResponsibleNode,
    repair_action: RepairAction,
    requires_user_confirmation: bool = False,
) -> ValidationIssue:
    return ValidationIssue(
        issue_id=_issue_id(plan.plan_id, rule_code),
        rule_code=rule_code,
        severity=severity,
        message=message,
        evidence=evidence,
        responsible_node=responsible_node,
        repairable=repair_action != RepairAction.NONE,
        repair_action=repair_action,
        requires_user_confirmation=requires_user_confirmation,
    )


def _budget_summary(
    request: TripRequest,
    plan: TripPlan,
) -> BudgetValidationSummary:
    budget = request.budget
    if budget is None:
        return BudgetValidationSummary(
            status=BudgetAssessmentStatus.NOT_REQUESTED,
            considered_cost_item_ids=(),
            excluded_cost_item_ids=tuple(item.cost_item_id for item in plan.cost_items),
            total_minimum=ZERO,
            total_maximum=ZERO,
            minimum_gap=ZERO,
            maximum_gap=ZERO,
        )

    included = set(budget.included_categories)
    considered = tuple(item for item in plan.cost_items if item.category in included)
    excluded = tuple(item for item in plan.cost_items if item.category not in included)
    present_categories = {item.category for item in considered}
    missing_categories = tuple(
        category for category in budget.included_categories if category not in present_categories
    )
    total_minimum = sum((item.total_minimum for item in considered), start=ZERO)
    total_maximum = sum((item.total_maximum for item in considered), start=ZERO)
    minimum_gap = max(total_minimum - budget.total_limit, ZERO)
    maximum_gap = max(total_maximum - budget.total_limit, ZERO)
    status = BudgetAssessmentStatus.WITHIN_LIMIT
    if minimum_gap > ZERO:
        status = BudgetAssessmentStatus.EXCEEDED
    elif missing_categories:
        status = BudgetAssessmentStatus.INCOMPLETE
    elif maximum_gap > ZERO:
        status = BudgetAssessmentStatus.POSSIBLE_OVERRUN
    return BudgetValidationSummary(
        status=status,
        total_limit=budget.total_limit,
        included_categories=budget.included_categories,
        missing_categories=missing_categories,
        considered_cost_item_ids=tuple(item.cost_item_id for item in considered),
        excluded_cost_item_ids=tuple(item.cost_item_id for item in excluded),
        total_minimum=total_minimum,
        total_maximum=total_maximum,
        minimum_gap=minimum_gap,
        maximum_gap=maximum_gap,
    )


def _budget_issue(
    request: TripRequest,
    plan: TripPlan,
    summary: BudgetValidationSummary,
) -> ValidationIssue | None:
    budget = request.budget
    if budget is None or summary.status in {
        BudgetAssessmentStatus.NOT_REQUESTED,
        BudgetAssessmentStatus.WITHIN_LIMIT,
    }:
        return None
    severity = IssueSeverity.ERROR if budget.hard_limit else IssueSeverity.WARNING
    if summary.status == BudgetAssessmentStatus.EXCEEDED:
        return _issue(
            plan,
            rule_code="budget.deterministic_floor_exceeds_limit",
            severity=severity,
            message="结构化费用下界已经超过预算上限。",
            evidence=(
                ValidationEvidence(
                    field_path="request.budget.total_limit",
                    description="用户预算上限",
                    observed_value=str(budget.total_limit),
                ),
                ValidationEvidence(
                    field_path="plan.cost_items.total_minimum",
                    description="预算范围内费用下界",
                    observed_value=str(summary.total_minimum),
                ),
                ValidationEvidence(
                    field_path="budget.minimum_gap",
                    description="确定性最小缺口",
                    observed_value=str(summary.minimum_gap),
                ),
            ),
            responsible_node=ResponsibleNode.BUDGET,
            repair_action=RepairAction.ASK_USER,
            requires_user_confirmation=True,
        )
    if summary.status == BudgetAssessmentStatus.INCOMPLETE:
        return _issue(
            plan,
            rule_code="budget.incomplete_category_coverage",
            severity=severity,
            message="预算包含的费用类别尚未全部形成 CostItem, 不能把缺失类别当作零元。",
            evidence=(
                ValidationEvidence(
                    field_path="budget.missing_categories",
                    description="尚无费用项的预算类别",
                    observed_value=",".join(item.value for item in summary.missing_categories),
                ),
            ),
            responsible_node=ResponsibleNode.BUDGET,
            repair_action=RepairAction.RECALCULATE_BUDGET,
        )
    return _issue(
        plan,
        rule_code="budget.possible_overrun",
        severity=severity,
        message="费用区间上界超过预算, 当前计划不能保证不超支。",
        evidence=(
            ValidationEvidence(
                field_path="request.budget.total_limit",
                description="用户预算上限",
                observed_value=str(budget.total_limit),
            ),
            ValidationEvidence(
                field_path="plan.cost_items.total_maximum",
                description="预算范围内费用上界",
                observed_value=str(summary.total_maximum),
            ),
            ValidationEvidence(
                field_path="budget.maximum_gap",
                description="最大可能超支",
                observed_value=str(summary.maximum_gap),
            ),
        ),
        responsible_node=ResponsibleNode.BUDGET,
        repair_action=RepairAction.RECALCULATE_BUDGET,
    )


def validate_trip_plan(request: TripRequest, plan: TripPlan) -> PlanValidationReport:
    issues: list[ValidationIssue] = []
    passed_rules: list[str] = []

    if plan.request_id != request.request_id:
        issues.append(
            _issue(
                plan,
                rule_code="plan.request_mismatch",
                severity=IssueSeverity.ERROR,
                message="TripPlan 不属于当前 TripRequest。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.request_id",
                        description="计划引用的请求 ID",
                        observed_value=plan.request_id,
                    ),
                    ValidationEvidence(
                        field_path="request.request_id",
                        description="当前请求 ID",
                        observed_value=request.request_id,
                    ),
                ),
                responsible_node=ResponsibleNode.PLAN,
                repair_action=RepairAction.NONE,
            )
        )
    else:
        passed_rules.append("plan.request_matches")

    if plan.destination_city != request.destination_city:
        issues.append(
            _issue(
                plan,
                rule_code="plan.destination_mismatch",
                severity=IssueSeverity.ERROR,
                message="计划目的地与请求目的地不一致。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.destination_city",
                        description="计划目的地",
                        observed_value=plan.destination_city,
                    ),
                    ValidationEvidence(
                        field_path="request.destination_city",
                        description="请求目的地",
                        observed_value=request.destination_city,
                    ),
                ),
                responsible_node=ResponsibleNode.PLAN,
                repair_action=RepairAction.NONE,
            )
        )
    else:
        passed_rules.append("plan.destination_matches")

    if plan.start_date != request.start_date or plan.end_date != request.end_date:
        issues.append(
            _issue(
                plan,
                rule_code="plan.date_window_mismatch",
                severity=IssueSeverity.ERROR,
                message="计划日期范围与请求不一致。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.date_window",
                        description="计划日期范围",
                        observed_value=f"{plan.start_date}/{plan.end_date}",
                    ),
                    ValidationEvidence(
                        field_path="request.date_window",
                        description="请求日期范围",
                        observed_value=f"{request.start_date}/{request.end_date}",
                    ),
                ),
                responsible_node=ResponsibleNode.PLAN,
                repair_action=RepairAction.NONE,
            )
        )
    else:
        passed_rules.append("plan.date_window_matches")

    candidate_ids = [
        item.candidate_id
        for day in plan.days
        for item in day.items
        if item.candidate_id is not None
    ]
    duplicates = tuple(
        sorted(candidate_id for candidate_id, count in Counter(candidate_ids).items() if count > 1)
    )
    if duplicates:
        issues.append(
            _issue(
                plan,
                rule_code="plan.duplicate_candidate",
                severity=IssueSeverity.ERROR,
                message="同一个候选地点被重复安排。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.days.items.candidate_id",
                        description="重复 candidate_id",
                        observed_value=",".join(duplicates),
                    ),
                ),
                responsible_node=ResponsibleNode.PLAN,
                repair_action=RepairAction.REPLAN_DAY,
            )
        )
    else:
        passed_rules.append("plan.candidate_ids_unique")

    grounded_kinds = {ActivityKind.ATTRACTION, ActivityKind.MEAL, ActivityKind.STAY}
    invalid_sources = tuple(
        item.item_id
        for day in plan.days
        for item in day.items
        if item.kind in grounded_kinds
        and (
            item.source is None
            or item.source.provider_id is None
            or item.source.data_mode not in {DataMode.LIVE, DataMode.FIXTURE}
        )
    )
    if invalid_sources:
        issues.append(
            _issue(
                plan,
                rule_code="source.invalid_grounding_mode",
                severity=IssueSeverity.ERROR,
                message="推荐活动必须来自 live 或 fixture provider 数据。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.days.items.source",
                        description="来源不满足 grounding 要求的 item_id",
                        observed_value=",".join(invalid_sources),
                    ),
                ),
                responsible_node=ResponsibleNode.VALIDATOR,
                repair_action=RepairAction.RERUN_EXPLORE,
            )
        )
    else:
        passed_rules.append("source.grounded_items_traceable")

    budget_summary = _budget_summary(request, plan)
    budget_issue = _budget_issue(request, plan, budget_summary)
    if budget_issue is None:
        passed_rules.append("budget.assessment_passed")
    else:
        issues.append(budget_issue)

    if plan.status == PlanStatus.CONFLICTED:
        issues.append(
            _issue(
                plan,
                rule_code="plan.preexisting_conflicted_status",
                severity=IssueSeverity.ERROR,
                message="输入计划已经标记为 conflicted, 不能进入最终定稿。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.status",
                        description="计划状态",
                        observed_value=plan.status.value,
                    ),
                ),
                responsible_node=ResponsibleNode.PLAN,
                repair_action=RepairAction.NONE,
            )
        )
    else:
        passed_rules.append("plan.not_preconflicted")

    if plan.status == PlanStatus.FINAL and any(
        issue.severity == IssueSeverity.ERROR for issue in issues
    ):
        issues.append(
            _issue(
                plan,
                rule_code="plan.finalized_with_errors",
                severity=IssueSeverity.ERROR,
                message="存在硬校验错误时不能把计划标记为 final。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.status",
                        description="计划状态",
                        observed_value=plan.status.value,
                    ),
                ),
                responsible_node=ResponsibleNode.PLAN,
                repair_action=RepairAction.NONE,
            )
        )
    else:
        passed_rules.append("plan.final_status_allowed")

    has_error = any(item.severity == IssueSeverity.ERROR for item in issues)
    has_warning = any(item.severity == IssueSeverity.WARNING for item in issues)
    status = PlanValidationStatus.PASSED
    if has_error:
        status = PlanValidationStatus.CONFLICTED
    elif has_warning:
        status = PlanValidationStatus.WARNING
    return PlanValidationReport(
        request_id=request.request_id,
        plan_id=plan.plan_id,
        status=status,
        can_finalize=not has_error,
        budget=budget_summary,
        issues=tuple(issues),
        passed_rule_codes=tuple(passed_rules),
    )
