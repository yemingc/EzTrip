import hashlib
import unicodedata
from collections import Counter
from datetime import date

from app.domain.opening_hours import OpeningHoursEvidence, OpeningHoursEvidenceBundle
from app.domain.planning import ActivityKind, PlanStatus, TripPlan
from app.domain.request import (
    Constraint,
    ConstraintKind,
    ConstraintStrength,
    TripRequest,
)
from app.domain.validation import (
    IssueSeverity,
    PlanValidationReport,
    PlanValidationStatus,
    RepairAction,
    ResponsibleNode,
    ValidationEvidence,
    ValidationIssue,
)
from app.planning.material_contracts import (
    PlanningMaterialBundle,
    RouteEdgeStatus,
)
from app.planning.validator import validate_trip_plan

HARD_VALIDATOR_VERSION = "hard-trip-plan-validator-v1"


def _issue_id(plan_id: str, rule_code: str) -> str:
    digest = hashlib.sha256(f"{plan_id}|{rule_code}".encode()).hexdigest()[:12]
    slug = rule_code.replace(".", "-").replace("_", "-")
    return f"issue-{slug}-{digest}"


def _issue(
    plan: TripPlan,
    *,
    rule_code: str,
    message: str,
    evidence: tuple[ValidationEvidence, ...],
    responsible_node: ResponsibleNode,
    repair_action: RepairAction,
) -> ValidationIssue:
    return ValidationIssue(
        issue_id=_issue_id(plan.plan_id, rule_code),
        rule_code=rule_code,
        severity=IssueSeverity.ERROR,
        message=message,
        evidence=evidence,
        responsible_node=responsible_node,
        repairable=repair_action != RepairAction.NONE,
        repair_action=repair_action,
    )


def _canonical_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith(("P", "Z"))
    )


def _constraint_values(constraint: Constraint) -> tuple[str, ...]:
    if isinstance(constraint.value, str):
        return (constraint.value,)
    if isinstance(constraint.value, list):
        return tuple(item for item in constraint.value if isinstance(item, str))
    return ()


def _constraint_applies(constraint: Constraint, scheduled_date: date) -> bool:
    return not constraint.applies_to_dates or scheduled_date in constraint.applies_to_dates


def validate_hard_trip_plan(
    request: TripRequest,
    plan: TripPlan,
    materials: PlanningMaterialBundle,
    opening_hours: OpeningHoursEvidenceBundle,
) -> PlanValidationReport:
    """Run the deterministic V1 finalization gate over a grounded TripPlan draft."""

    base_report = validate_trip_plan(request, plan)
    issues = list(base_report.issues)
    passed_rules = list(base_report.passed_rule_codes)

    def add_issue(issue: ValidationIssue) -> None:
        if issue.rule_code not in {item.rule_code for item in issues}:
            issues.append(issue)
        if issue.rule_code in passed_rules:
            passed_rules.remove(issue.rule_code)

    def pass_rule(rule_code: str) -> None:
        if rule_code not in {item.rule_code for item in issues} and rule_code not in passed_rules:
            passed_rules.append(rule_code)

    if materials.request_id != request.request_id:
        add_issue(
            _issue(
                plan,
                rule_code="materials.request_mismatch",
                message="规划材料不属于当前旅行请求。",
                evidence=(
                    ValidationEvidence(
                        field_path="materials.request_id",
                        description="规划材料引用的请求 ID",
                        observed_value=materials.request_id,
                    ),
                ),
                responsible_node=ResponsibleNode.VALIDATOR,
                repair_action=RepairAction.NONE,
            )
        )
    else:
        pass_rule("materials.request_matches")

    if opening_hours.request_id != request.request_id:
        add_issue(
            _issue(
                plan,
                rule_code="opening_hours.request_mismatch",
                message="营业时间证据不属于当前旅行请求。",
                evidence=(
                    ValidationEvidence(
                        field_path="opening_hours.request_id",
                        description="营业时间证据引用的请求 ID",
                        observed_value=opening_hours.request_id,
                    ),
                ),
                responsible_node=ResponsibleNode.VALIDATOR,
                repair_action=RepairAction.NONE,
            )
        )
    else:
        pass_rule("opening_hours.request_matches")

    if opening_hours.data_mode != materials.data_mode:
        add_issue(
            _issue(
                plan,
                rule_code="opening_hours.data_mode_mismatch",
                message="营业时间证据与规划材料的数据模式不一致。",
                evidence=(
                    ValidationEvidence(
                        field_path="opening_hours.data_mode",
                        description="营业时间证据的数据模式",
                        observed_value=opening_hours.data_mode.value,
                    ),
                    ValidationEvidence(
                        field_path="materials.data_mode",
                        description="规划材料的数据模式",
                        observed_value=materials.data_mode.value,
                    ),
                ),
                responsible_node=ResponsibleNode.VALIDATOR,
                repair_action=RepairAction.NONE,
            )
        )
    else:
        pass_rule("opening_hours.data_mode_matches")

    candidates_by_id = {
        candidate.candidate_id: candidate for candidate in materials.shortlist.poi_candidates
    }
    scheduled_items = tuple(
        item
        for day in plan.days
        for item in day.items
        if item.kind in {ActivityKind.ATTRACTION, ActivityKind.MEAL}
    )
    scheduled_ids = tuple(
        item.candidate_id for item in scheduled_items if item.candidate_id is not None
    )
    expected_ids = tuple(candidates_by_id)
    scheduled_counts = Counter(scheduled_ids)
    missing_ids = tuple(item for item in expected_ids if scheduled_counts[item] == 0)
    unexpected_ids = tuple(item for item in scheduled_ids if item not in candidates_by_id)
    repeated_ids = tuple(
        sorted(candidate_id for candidate_id, count in scheduled_counts.items() if count > 1)
    )
    if missing_ids or unexpected_ids or repeated_ids:
        add_issue(
            _issue(
                plan,
                rule_code="plan.candidate_scope_mismatch",
                message="计划必须恰好覆盖规划 shortlist 中的候选地点。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.days.items.candidate_id",
                        description="缺失、越界、重复候选",
                        observed_value=(
                            f"missing={','.join(missing_ids)};"
                            f"unexpected={','.join(unexpected_ids)};"
                            f"repeated={','.join(repeated_ids)}"
                        ),
                    ),
                ),
                responsible_node=ResponsibleNode.PLAN,
                repair_action=RepairAction.REPLAN_DAY,
            )
        )
    else:
        pass_rule("plan.candidate_scope_exact")

    lineage_mismatches = tuple(
        item.item_id
        for item in scheduled_items
        if item.candidate_id in candidates_by_id
        and (
            item.title != candidates_by_id[item.candidate_id].name
            or item.source != candidates_by_id[item.candidate_id].source
        )
    )
    if lineage_mismatches:
        add_issue(
            _issue(
                plan,
                rule_code="source.candidate_lineage_mismatch",
                message="计划中的候选名称或来源与 shortlist 事实不一致。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.days.items.source",
                        description="候选事实血缘不一致的 item_id",
                        observed_value=",".join(lineage_mismatches),
                    ),
                ),
                responsible_node=ResponsibleNode.VALIDATOR,
                repair_action=RepairAction.NONE,
            )
        )
    else:
        pass_rule("source.candidate_lineage_matches")

    wrong_city_pois = tuple(
        candidate.candidate_id
        for candidate in materials.shortlist.poi_candidates
        if candidate.city != request.destination_city
    )
    if wrong_city_pois:
        add_issue(
            _issue(
                plan,
                rule_code="city.poi_candidate_mismatch",
                message="景点候选城市与请求目的地不一致。",
                evidence=(
                    ValidationEvidence(
                        field_path="materials.shortlist.poi_candidates.city",
                        description="跨城 POI candidate_id",
                        observed_value=",".join(wrong_city_pois),
                    ),
                ),
                responsible_node=ResponsibleNode.EXPLORE,
                repair_action=RepairAction.RERUN_EXPLORE,
            )
        )
    else:
        pass_rule("city.poi_candidates_match")

    stay = materials.shortlist.primary_stay
    if stay is not None and stay.city != request.destination_city:
        add_issue(
            _issue(
                plan,
                rule_code="city.stay_candidate_mismatch",
                message="住宿锚点城市与请求目的地不一致。",
                evidence=(
                    ValidationEvidence(
                        field_path="materials.shortlist.primary_stay.city",
                        description="住宿锚点城市",
                        observed_value=stay.city,
                    ),
                ),
                responsible_node=ResponsibleNode.STAY,
                repair_action=RepairAction.RERUN_STAY,
            )
        )
    else:
        pass_rule("city.stay_candidate_matches")

    hard_constraints = tuple(
        constraint
        for constraint in request.constraints.items
        if constraint.confirmed and constraint.strength == ConstraintStrength.HARD
    )
    scheduled_by_name: dict[str, list[tuple[date, str]]] = {}
    for day in plan.days:
        for item in day.items:
            if item.kind not in {ActivityKind.ATTRACTION, ActivityKind.MEAL}:
                continue
            scheduled_by_name.setdefault(_canonical_name(item.title), []).append(
                (day.date, item.item_id)
            )

    missing_must_visit: list[str] = []
    scheduled_avoid: list[str] = []
    for constraint in hard_constraints:
        for value in _constraint_values(constraint):
            matching_items = scheduled_by_name.get(_canonical_name(value), [])
            applicable_items = tuple(
                item for item in matching_items if _constraint_applies(constraint, item[0])
            )
            if constraint.kind == ConstraintKind.MUST_VISIT and not applicable_items:
                missing_must_visit.append(f"{constraint.constraint_id}:{value}")
            if constraint.kind == ConstraintKind.AVOID and applicable_items:
                scheduled_avoid.append(
                    f"{constraint.constraint_id}:{value}:"
                    f"{','.join(item[1] for item in applicable_items)}"
                )

    if missing_must_visit:
        add_issue(
            _issue(
                plan,
                rule_code="constraint.hard_must_visit_missing",
                message="计划没有满足已确认的硬性必去地点。",
                evidence=(
                    ValidationEvidence(
                        field_path="request.constraints.must_visit",
                        description="未满足的 constraint_id 与规范地点名",
                        observed_value=";".join(missing_must_visit),
                    ),
                ),
                responsible_node=ResponsibleNode.EXPLORE,
                repair_action=RepairAction.RERUN_EXPLORE,
            )
        )
    else:
        pass_rule("constraint.hard_must_visit_satisfied")

    if scheduled_avoid:
        add_issue(
            _issue(
                plan,
                rule_code="constraint.hard_avoid_scheduled",
                message="计划安排了用户明确要求避开的地点。",
                evidence=(
                    ValidationEvidence(
                        field_path="request.constraints.avoid",
                        description="命中的 constraint_id、地点和 item_id",
                        observed_value=";".join(scheduled_avoid),
                    ),
                ),
                responsible_node=ResponsibleNode.EXPLORE,
                repair_action=RepairAction.RERUN_EXPLORE,
            )
        )
    else:
        pass_rule("constraint.hard_avoid_respected")

    route_edges = {
        (edge.origin_candidate_id, edge.destination_candidate_id): edge
        for edge in materials.route_matrix.edges
    }
    missing_routes: list[str] = []
    endpoint_mismatches: list[str] = []
    lineage_route_mismatches: list[str] = []
    transfer_conflicts: list[str] = []
    for day in plan.days:
        previous_item = None
        for item in day.items:
            if item.kind not in {ActivityKind.ATTRACTION, ActivityKind.MEAL}:
                continue
            expected_origin_id = (
                previous_item.candidate_id
                if previous_item is not None
                else (stay.candidate_id if stay is not None else None)
            )
            route = item.route_from_previous
            if route is None:
                missing_routes.append(item.item_id)
                previous_item = item
                continue
            if (
                route.origin.candidate_id != expected_origin_id
                or route.destination.candidate_id != item.candidate_id
            ):
                endpoint_mismatches.append(item.item_id)
            edge = (
                route_edges.get((expected_origin_id, item.candidate_id))
                if expected_origin_id is not None and item.candidate_id is not None
                else None
            )
            if edge is None or edge.status != RouteEdgeStatus.SUCCEEDED or edge.route != route:
                lineage_route_mismatches.append(item.item_id)
            if previous_item is not None:
                available_minutes = int(
                    (item.start_at - previous_item.end_at).total_seconds() // 60
                )
                if route.duration_minutes > available_minutes:
                    transfer_conflicts.append(
                        f"{item.item_id}:required={route.duration_minutes}:available={available_minutes}"
                    )
            previous_item = item

    if missing_routes:
        add_issue(
            _issue(
                plan,
                rule_code="route.missing_for_grounded_item",
                message="带来源的行程活动缺少到达路线。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.days.items.route_from_previous",
                        description="缺少路线的 item_id",
                        observed_value=",".join(missing_routes),
                    ),
                ),
                responsible_node=ResponsibleNode.ROUTE,
                repair_action=RepairAction.RERUN_ROUTE,
            )
        )
    else:
        pass_rule("route.present_for_grounded_items")

    if endpoint_mismatches:
        add_issue(
            _issue(
                plan,
                rule_code="route.endpoint_mismatch",
                message="路线起终点与相邻行程活动不一致。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.days.items.route_from_previous",
                        description="路线端点不一致的 item_id",
                        observed_value=",".join(endpoint_mismatches),
                    ),
                ),
                responsible_node=ResponsibleNode.ROUTE,
                repair_action=RepairAction.RERUN_ROUTE,
            )
        )
    else:
        pass_rule("route.endpoints_match_timeline")

    if lineage_route_mismatches:
        add_issue(
            _issue(
                plan,
                rule_code="route.lineage_mismatch",
                message="计划路线与路线矩阵中的 Provider 事实不一致。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.days.items.route_from_previous.source",
                        description="路线血缘不一致的 item_id",
                        observed_value=",".join(lineage_route_mismatches),
                    ),
                ),
                responsible_node=ResponsibleNode.ROUTE,
                repair_action=RepairAction.RERUN_ROUTE,
            )
        )
    else:
        pass_rule("route.lineage_matches_matrix")

    if transfer_conflicts:
        add_issue(
            _issue(
                plan,
                rule_code="route.insufficient_transfer_window",
                message="相邻活动之间没有预留足够的路线通行时间。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.days.items.start_at",
                        description="需要分钟数大于可用分钟数的 item_id",
                        observed_value=";".join(transfer_conflicts),
                    ),
                ),
                responsible_node=ResponsibleNode.PLAN,
                repair_action=RepairAction.REPLAN_DAY,
            )
        )
    else:
        pass_rule("route.transfer_windows_feasible")

    evidence_by_candidate_date: dict[tuple[str, date], list[OpeningHoursEvidence]] = {}
    if opening_hours.request_id == request.request_id:
        for evidence in opening_hours.items:
            evidence_by_candidate_date.setdefault(
                (evidence.candidate_id, evidence.service_date), []
            ).append(evidence)
    missing_opening_evidence: list[str] = []
    outside_opening_window: list[str] = []
    for day in plan.days:
        for item in day.items:
            if (
                item.kind not in {ActivityKind.ATTRACTION, ActivityKind.MEAL}
                or item.candidate_id is None
            ):
                continue
            windows = evidence_by_candidate_date.get((item.candidate_id, day.date), [])
            if not windows:
                missing_opening_evidence.append(item.item_id)
                continue
            if not any(
                evidence.opens_at <= item.start_at and item.end_at <= evidence.closes_at
                for evidence in windows
            ):
                outside_opening_window.append(item.item_id)

    if missing_opening_evidence:
        add_issue(
            _issue(
                plan,
                rule_code="opening_hours.evidence_missing",
                message="活动缺少对应日期且可追溯的营业时间证据。",
                evidence=(
                    ValidationEvidence(
                        field_path="opening_hours.items",
                        description="缺少营业时间证据的 item_id",
                        observed_value=",".join(missing_opening_evidence),
                    ),
                ),
                responsible_node=ResponsibleNode.EXPLORE,
                repair_action=RepairAction.RERUN_EXPLORE,
            )
        )
    else:
        pass_rule("opening_hours.evidence_complete")

    if outside_opening_window:
        add_issue(
            _issue(
                plan,
                rule_code="opening_hours.schedule_outside_verified_window",
                message="活动时间不在任何已验证的营业窗口内。",
                evidence=(
                    ValidationEvidence(
                        field_path="plan.days.items.time_window",
                        description="超出营业窗口的 item_id",
                        observed_value=",".join(outside_opening_window),
                    ),
                ),
                responsible_node=ResponsibleNode.PLAN,
                repair_action=RepairAction.REPLAN_DAY,
            )
        )
    else:
        pass_rule("opening_hours.schedule_within_verified_windows")

    hard_budget_error = any(
        issue.rule_code.startswith("budget.") and issue.severity == IssueSeverity.ERROR
        for issue in issues
    )
    if hard_budget_error:
        if "hard_budget.assessment_allows_finalization" in passed_rules:
            passed_rules.remove("hard_budget.assessment_allows_finalization")
    else:
        pass_rule("hard_budget.assessment_allows_finalization")

    if plan.status == PlanStatus.FINAL and any(
        issue.severity == IssueSeverity.ERROR for issue in issues
    ):
        if "plan.final_status_allowed" in passed_rules:
            passed_rules.remove("plan.final_status_allowed")
        add_issue(
            _issue(
                plan,
                rule_code="plan.finalized_with_errors",
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

    has_error = any(item.severity == IssueSeverity.ERROR for item in issues)
    has_warning = any(item.severity == IssueSeverity.WARNING for item in issues)
    status = PlanValidationStatus.PASSED
    if has_error:
        status = PlanValidationStatus.CONFLICTED
    elif has_warning:
        status = PlanValidationStatus.WARNING
    return PlanValidationReport(
        validator_version=HARD_VALIDATOR_VERSION,
        request_id=request.request_id,
        plan_id=plan.plan_id,
        status=status,
        can_finalize=not has_error,
        budget=base_report.budget,
        issues=tuple(issues),
        passed_rule_codes=tuple(passed_rules),
    )
