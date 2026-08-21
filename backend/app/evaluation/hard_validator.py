import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.agents.plan_agent import run_plan_agent
from app.domain.opening_hours import OpeningHoursEvidence, OpeningHoursEvidenceBundle
from app.domain.planning import ActivityKind, DayPlan, ItineraryItem, TripPlan
from app.domain.request import (
    Constraint,
    ConstraintKind,
    ConstraintSet,
    ConstraintSource,
    ConstraintStrength,
    TripRequest,
)
from app.domain.sources import DataMode, SourceReference
from app.domain.validation import ValidationIssue
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.hard_validator_contracts import (
    ExpectedValidationIssue,
    HardConstraintScenario,
    HardMaterialMutation,
    HardPlanMutation,
    HardValidatorBaselineReport,
    HardValidatorCaseResult,
    HardValidatorEvalCase,
    HardValidatorEvalSuite,
    OpeningEvidenceScenario,
)
from app.evaluation.plan_agent import (
    PlanAgentFixtureModel,
    build_plan_agent_materials,
    load_plan_agent_suite,
)
from app.evaluation.plan_agent_contracts import PlanAgentEvalCase
from app.planning.hard_validator import validate_hard_trip_plan
from app.planning.material_contracts import PlanningMaterialBundle

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HARD_VALIDATOR_SUITE_PATH = REPOSITORY_ROOT / "evals" / "cases" / "hard-validator" / "suite.v1.json"


class HardValidatorEvaluationError(RuntimeError):
    """Raised when the hard-validator fixture inventory contradicts its references."""


def load_hard_validator_suite(
    suite_path: Path = HARD_VALIDATOR_SUITE_PATH,
) -> HardValidatorEvalSuite:
    return HardValidatorEvalSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))


def _source_cases() -> dict[str, PlanAgentEvalCase]:
    return {item.case_id: item for item in load_plan_agent_suite().cases}


def hard_validator_dataset_sha256(suite: HardValidatorEvalSuite) -> str:
    source_cases = _source_cases()
    references: list[dict[str, object]] = []
    for case in suite.cases:
        try:
            source_case = source_cases[case.source_plan_case_id]
        except KeyError as error:
            raise HardValidatorEvaluationError(
                f"unknown Plan Agent case reference: {error.args[0]}"
            ) from error
        references.append(source_case.model_dump(mode="json"))
    canonical = json.dumps(
        {
            "suite": suite.model_dump(mode="json"),
            "source_plan_cases": references,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _constraint(
    *,
    constraint_id: str,
    kind: ConstraintKind,
    value: str,
) -> Constraint:
    return Constraint(
        constraint_id=constraint_id,
        kind=kind,
        value=value,
        strength=ConstraintStrength.HARD,
        priority=5,
        source=ConstraintSource.USER_EXPLICIT,
        confirmed=True,
    )


def _mutate_request(
    request: TripRequest,
    materials: PlanningMaterialBundle,
    scenario: HardConstraintScenario,
) -> TripRequest:
    if scenario == HardConstraintScenario.PRESERVE:
        return request
    payload = request.model_dump(mode="python")
    if scenario == HardConstraintScenario.HARD_BUDGET:
        if request.budget is None:
            raise HardValidatorEvaluationError("hard-budget scenario requires an existing budget")
        payload["budget"] = {
            **request.budget.model_dump(mode="python"),
            "hard_limit": True,
        }
        return TripRequest.model_validate(payload)

    constraints = list(request.constraints.items)
    if scenario == HardConstraintScenario.MISSING_MUST_VISIT:
        constraints.append(
            _constraint(
                constraint_id="hard_validator_missing_must",
                kind=ConstraintKind.MUST_VISIT,
                value="未入选测试景点",
            )
        )
    else:
        if not materials.shortlist.poi_candidates:
            raise HardValidatorEvaluationError("scheduled-avoid scenario requires a POI")
        constraints.append(
            _constraint(
                constraint_id="hard_validator_scheduled_avoid",
                kind=ConstraintKind.AVOID,
                value=materials.shortlist.poi_candidates[0].name,
            )
        )
    payload["constraints"] = ConstraintSet(items=tuple(constraints))
    return TripRequest.model_validate(payload)


def _replace_poi_city(
    payload: dict[str, object],
    target_id: str,
    city: str,
) -> None:
    shortlist = payload["shortlist"]
    assert isinstance(shortlist, dict)
    candidates = shortlist["poi_candidates"]
    assert isinstance(candidates, (list, tuple))
    for candidate in candidates:
        assert isinstance(candidate, dict)
        if candidate["candidate_id"] == target_id:
            candidate["city"] = city

    specialist = payload["specialist_result"]
    assert isinstance(specialist, dict)
    branches = specialist["branches"]
    assert isinstance(branches, (list, tuple))
    for branch in branches:
        assert isinstance(branch, dict)
        result = branch.get("explore_result")
        if not isinstance(result, dict):
            continue
        for collection_name in ("observations", "recommendations"):
            collection = result[collection_name]
            assert isinstance(collection, (list, tuple))
            for record in collection:
                assert isinstance(record, dict)
                candidate = record["candidate"]
                assert isinstance(candidate, dict)
                if candidate["candidate_id"] == target_id:
                    candidate["city"] = city


def _replace_stay_city(
    payload: dict[str, object],
    target_id: str,
    city: str,
) -> None:
    shortlist = payload["shortlist"]
    assert isinstance(shortlist, dict)
    stay = shortlist["primary_stay"]
    assert isinstance(stay, dict)
    stay["city"] = city

    specialist = payload["specialist_result"]
    assert isinstance(specialist, dict)
    branches = specialist["branches"]
    assert isinstance(branches, (list, tuple))
    for branch in branches:
        assert isinstance(branch, dict)
        result = branch.get("stay_result")
        if not isinstance(result, dict):
            continue
        for collection_name in ("observations", "recommendations"):
            collection = result[collection_name]
            assert isinstance(collection, (list, tuple))
            for record in collection:
                assert isinstance(record, dict)
                candidate = record["candidate"]
                assert isinstance(candidate, dict)
                if candidate["candidate_id"] == target_id:
                    candidate["city"] = city


def _mutate_materials(
    materials: PlanningMaterialBundle,
    mutation: HardMaterialMutation,
) -> PlanningMaterialBundle:
    if mutation == HardMaterialMutation.NONE:
        return materials
    payload = materials.model_dump(mode="python")
    if mutation == HardMaterialMutation.POI_CROSS_CITY:
        if not materials.shortlist.poi_candidates:
            raise HardValidatorEvaluationError("POI cross-city scenario requires a POI")
        _replace_poi_city(
            payload,
            materials.shortlist.poi_candidates[0].candidate_id,
            "天津市",
        )
    else:
        if materials.shortlist.primary_stay is None:
            raise HardValidatorEvaluationError("stay cross-city scenario requires a stay")
        _replace_stay_city(
            payload,
            materials.shortlist.primary_stay.candidate_id,
            "天津市",
        )
    return PlanningMaterialBundle.model_validate(payload)


def _first_grounded_item_payload(payload: dict[str, object]) -> dict[str, object]:
    days = payload["days"]
    assert isinstance(days, (list, tuple))
    for day in days:
        assert isinstance(day, dict)
        items = day["items"]
        assert isinstance(items, (list, tuple))
        for item in items:
            assert isinstance(item, dict)
            if item.get("candidate_id") is not None:
                return item
    raise HardValidatorEvaluationError("plan mutation requires a grounded itinerary item")


def _mutate_plan(plan: TripPlan, mutation: HardPlanMutation) -> TripPlan:
    if mutation == HardPlanMutation.NONE:
        return plan
    payload = plan.model_dump(mode="python")
    if mutation == HardPlanMutation.TIGHT_TRANSFER:
        days = payload["days"]
        assert isinstance(days, (list, tuple))
        for day in days:
            assert isinstance(day, dict)
            items = day["items"]
            assert isinstance(items, (list, tuple))
            grounded = [item for item in items if item.get("candidate_id") is not None]
            if len(grounded) < 2:
                continue
            first, second = grounded[:2]
            first_end = first["end_at"]
            assert isinstance(first_end, datetime)
            second["start_at"] = first_end + timedelta(minutes=30)
            second["end_at"] = second["start_at"] + timedelta(hours=2)
            return TripPlan.model_validate(payload)
        raise HardValidatorEvaluationError("tight-transfer scenario requires two same-day POIs")

    item = _first_grounded_item_payload(payload)
    if mutation == HardPlanMutation.MISSING_ROUTE:
        item["route_from_previous"] = None
    elif mutation == HardPlanMutation.CANDIDATE_SOURCE_MISMATCH:
        source = item["source"]
        assert isinstance(source, dict)
        source["provider_id"] = f"{source['provider_id']}-mismatch"
    else:
        route = item["route_from_previous"]
        assert isinstance(route, dict)
        source = route["source"]
        assert isinstance(source, dict)
        source["provider_id"] = f"{source['provider_id']}-mismatch"
    return TripPlan.model_validate(payload)


def _grounded_items(plan: TripPlan) -> tuple[tuple[DayPlan, ItineraryItem], ...]:
    return tuple(
        (day, item)
        for day in plan.days
        for item in day.items
        if item.kind in {ActivityKind.ATTRACTION, ActivityKind.MEAL}
        and item.candidate_id is not None
    )


def _opening_hours(
    request: TripRequest,
    plan: TripPlan,
    scenario: OpeningEvidenceScenario,
) -> OpeningHoursEvidenceBundle:
    grounded = _grounded_items(plan)
    items: list[OpeningHoursEvidence] = []
    for index, (day, item) in enumerate(grounded):
        if scenario == OpeningEvidenceScenario.MISSING_ONE and index == 0:
            continue
        opens_at = item.start_at - timedelta(hours=1)
        closes_at = item.end_at + timedelta(hours=1)
        if scenario == OpeningEvidenceScenario.OUTSIDE_ONE and index == 0:
            opens_at = item.start_at - timedelta(hours=3)
            closes_at = item.start_at - timedelta(hours=1)
        assert item.candidate_id is not None
        items.append(
            OpeningHoursEvidence(
                evidence_id=f"opening-{item.item_id}",
                candidate_id=item.candidate_id,
                service_date=day.date,
                opens_at=opens_at,
                closes_at=closes_at,
                source=SourceReference(
                    provider="hard-validator-opening-fixture",
                    provider_id=f"opening-source-{item.item_id}",
                    data_mode=DataMode.FIXTURE,
                    retrieved_at=datetime(2026, 9, 20, tzinfo=UTC),
                ),
            )
        )
    return OpeningHoursEvidenceBundle(
        request_id=request.request_id,
        data_mode=DataMode.FIXTURE,
        items=tuple(items),
    )


def _issue_labels(
    report_issues: tuple[ValidationIssue, ...],
) -> tuple[ExpectedValidationIssue, ...]:
    return tuple(
        ExpectedValidationIssue(
            rule_code=issue.rule_code,
            severity=issue.severity,
            responsible_node=issue.responsible_node,
            repair_action=issue.repair_action,
        )
        for issue in report_issues
    )


def _stable_error_code(error: Exception) -> str:
    material = f"{error.__class__.__module__}|{error.__class__.__qualname__}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"hard-validator-error-{digest}"


def _failed_result(
    case: HardValidatorEvalCase,
    error: Exception,
) -> HardValidatorCaseResult:
    return HardValidatorCaseResult(
        case_id=case.case_id,
        passed=False,
        expected_status=case.expected.status,
        actual_status=case.expected.status,
        expected_can_finalize=case.expected.can_finalize,
        actual_can_finalize=case.expected.can_finalize,
        expected_issues=case.expected.issues,
        actual_issues=(),
        routing_match_count=0,
        deterministic_replay=False,
        error_code=_stable_error_code(error),
        checks=(EvaluationCheck(code="workflow_completed", passed=False),),
    )


async def evaluate_hard_validator_case(
    case: HardValidatorEvalCase,
) -> HardValidatorCaseResult:
    try:
        try:
            source_case = _source_cases()[case.source_plan_case_id]
        except KeyError as error:
            raise HardValidatorEvaluationError(
                f"unknown Plan Agent case reference: {error.args[0]}"
            ) from error
        base_materials = await build_plan_agent_materials(source_case)
        request = _mutate_request(
            source_case.request,
            base_materials,
            case.constraint_scenario,
        )
        if request != source_case.request:
            source_case = source_case.model_copy(update={"request": request})
            base_materials = await build_plan_agent_materials(source_case)
        plan_result = run_plan_agent(request, base_materials, PlanAgentFixtureModel())
        if plan_result.plan is None:
            raise HardValidatorEvaluationError("hard-validator case requires a Plan draft")
        materials = _mutate_materials(base_materials, case.material_mutation)
        plan = _mutate_plan(plan_result.plan, case.plan_mutation)
        opening_hours = _opening_hours(request, plan, case.opening_evidence)
        first = validate_hard_trip_plan(request, plan, materials, opening_hours)
        second = validate_hard_trip_plan(request, plan, materials, opening_hours)
    except Exception as error:
        return _failed_result(case, error)

    actual_issues = _issue_labels(first.issues)
    expected_routes = {
        (item.rule_code, item.severity, item.responsible_node, item.repair_action)
        for item in case.expected.issues
    }
    actual_routes = {
        (item.rule_code, item.severity, item.responsible_node, item.repair_action)
        for item in actual_issues
    }
    expected_rule_codes = {item.rule_code for item in case.expected.issues}
    actual_rule_codes = {item.rule_code for item in actual_issues}
    routing_match_count = len(expected_routes & actual_routes)
    checks = (
        EvaluationCheck(code="workflow_completed", passed=True),
        EvaluationCheck(code="validator_zero_model_calls", passed=True),
        EvaluationCheck(
            code="status_matches",
            passed=first.status == case.expected.status,
        ),
        EvaluationCheck(
            code="finalization_matches",
            passed=first.can_finalize == case.expected.can_finalize,
        ),
        EvaluationCheck(
            code="exact_issue_set",
            passed=actual_rule_codes == expected_rule_codes,
        ),
        EvaluationCheck(
            code="exact_issue_routing",
            passed=actual_routes == expected_routes,
        ),
        EvaluationCheck(code="deterministic_replay", passed=first == second),
    )
    return HardValidatorCaseResult(
        case_id=case.case_id,
        passed=all(item.passed for item in checks),
        expected_status=case.expected.status,
        actual_status=first.status,
        expected_can_finalize=case.expected.can_finalize,
        actual_can_finalize=first.can_finalize,
        expected_issues=case.expected.issues,
        actual_issues=actual_issues,
        routing_match_count=routing_match_count,
        deterministic_replay=first == second,
        checks=checks,
    )


async def evaluate_hard_validator_suite(
    suite_path: Path = HARD_VALIDATOR_SUITE_PATH,
) -> HardValidatorBaselineReport:
    suite = load_hard_validator_suite(suite_path)
    results = tuple([await evaluate_hard_validator_case(case) for case in suite.cases])
    exact_issue_set_case_count = sum(
        {item.rule_code for item in result.expected_issues}
        == {item.rule_code for item in result.actual_issues}
        for result in results
    )
    expected_issue_count = sum(len(item.expected_issues) for item in results)
    routing_match_count = sum(item.routing_match_count for item in results)
    passed_case_count = sum(item.passed for item in results)
    return HardValidatorBaselineReport(
        dataset_sha256=hard_validator_dataset_sha256(suite),
        passed_case_count=passed_case_count,
        case_pass_rate=expected_rate(passed_case_count, len(results)),
        exact_issue_set_case_count=exact_issue_set_case_count,
        exact_issue_set_rate=expected_rate(exact_issue_set_case_count, len(results)),
        expected_issue_count=expected_issue_count,
        routing_match_count=routing_match_count,
        routing_accuracy=expected_rate(routing_match_count, expected_issue_count),
        deterministic_replay_case_count=sum(item.deterministic_replay for item in results),
        results=results,
        limitations=(
            "所有 POI、住宿、路线与营业时间均为显式 fixture, 不代表当前实时 Provider 数据。",
            "营业时间只验证给定窗口与排程的机械关系; 当前高德候选尚未稳定提供该证据。",
            "must/avoid V1 使用规范化后的精确地点名匹配, 别名消歧仍需上游约束绑定。",
            "预算沿用 CostItem 事实边界; 缺少价格时硬预算阻断, 软预算只告警。",
            "本报告验证规则定位与责任路由, Repair Router 尚未执行任何自动修复。",
        ),
    )
