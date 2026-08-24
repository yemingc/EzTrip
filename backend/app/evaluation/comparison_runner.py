import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from app.agents.contracts import (
    ExploreAgentResult,
    ModelTokenUsage,
    PlannerModelResponse,
    PlannerPlacementProposal,
    PlannerProposalBatch,
    StayAgentResult,
)
from app.agents.plan_agent import normalize_plan_response, run_plan_agent
from app.agents.plan_agent_contracts import PlanAgentRunResult, PlanAgentRunStatus
from app.domain.candidates import ActivityEnvironment
from app.domain.context import PlannerContext
from app.domain.money import BudgetCategory, CostItem, MoneyRange
from app.domain.opening_hours import OpeningHoursEvidence, OpeningHoursEvidenceBundle
from app.domain.planning import ActivityKind, TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode, SourceReference
from app.domain.validation import (
    IssueSeverity,
    PlanValidationReport,
)
from app.evaluation.comparison import (
    ComparisonEvaluationError,
    comparison_dataset_sha256,
    load_comparison_suite,
)
from app.evaluation.comparison_contracts import (
    ComparisonArm,
    ComparisonEvalCase,
    ComparisonOutcome,
    ComparisonScenario,
)
from app.evaluation.comparison_run_contracts import (
    ComparisonArmCaseResult,
    ComparisonArmSummary,
    ComparisonPairedDelta,
    ComparisonRunOutput,
    ComparisonToolSnapshot,
    SystemComparisonReport,
)
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.hard_validator import (
    _mutate_materials,
    _mutate_plan,
    _mutate_request,
)
from app.evaluation.hard_validator_contracts import (
    HardConstraintScenario,
    HardMaterialMutation,
    HardPlanMutation,
)
from app.evaluation.plan_agent import build_plan_agent_materials, load_plan_agent_suite
from app.evaluation.plan_agent_contracts import PlanAgentEvalCase
from app.planning.hard_validator import validate_hard_trip_plan
from app.planning.material_contracts import (
    PlanningMaterialBundle,
    planning_materials_support_draft,
)
from app.planning.product_repair import ProductRepairExecutor
from app.planning.repair_contracts import RepairOutcome, RepairRouterResult
from app.planning.repair_router import run_repair_router
from app.planning.specialist_contracts import (
    SpecialistFanoutResult,
    SpecialistName,
)

COMPARISON_FIXTURE_MODEL = "fixture-comparison-policy-v1"


def _sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_comparison_tool_snapshot(
    materials: PlanningMaterialBundle,
) -> ComparisonToolSnapshot:
    if materials.data_mode != DataMode.FIXTURE:
        raise ComparisonEvaluationError("comparison tool snapshot is fixture-only")
    weather = tuple(
        risk for branch in materials.specialist_result.branches for risk in branch.weather_risks
    )
    stay_branch = next(
        item
        for item in materials.specialist_result.branches
        if item.specialist == SpecialistName.STAY
    )
    stay_candidates = (
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
    route_anchor_candidate_id = (
        materials.shortlist.primary_stay.candidate_id
        if materials.shortlist.primary_stay is not None
        else None
    )
    route_payload = materials.route_matrix.model_dump(mode="json")
    route_payload["latency_ms"] = 0
    serialized = {
        "schema_version": "1.0",
        "snapshot_version": "comparison-tool-snapshot-v1",
        "request_id": materials.request_id,
        "context_id": materials.context_id,
        "data_mode": DataMode.FIXTURE.value,
        "planner_context": materials.planner_context.model_dump(mode="json"),
        "poi_candidates": [
            item.model_dump(mode="json") for item in materials.shortlist.poi_candidates
        ],
        "stay_candidates": [item.model_dump(mode="json") for item in stay_candidates],
        "route_anchor_candidate_id": route_anchor_candidate_id,
        "weather_risks": [item.model_dump(mode="json") for item in weather],
        "route_matrix": route_payload,
        "budget_allocation": materials.budget_allocation.model_dump(mode="json"),
    }
    return ComparisonToolSnapshot(
        request_id=materials.request_id,
        context_id=materials.context_id,
        data_mode=DataMode.FIXTURE,
        planner_context=materials.planner_context,
        poi_candidates=materials.shortlist.poi_candidates,
        stay_candidates=stay_candidates,
        route_anchor_candidate_id=route_anchor_candidate_id,
        weather_risks=weather,
        route_matrix=materials.route_matrix,
        budget_allocation=materials.budget_allocation,
        snapshot_sha256=_sha256(serialized),
    )


class ComparisonFixtureSingleAgentPolicy:
    """One fixture policy over all frozen tool facts; no specialist or repair loop."""

    def propose(self, snapshot: ComparisonToolSnapshot) -> PlannerModelResponse:
        weather_dates = {
            risk.starts_at.date() + timedelta(days=offset)
            for risk in snapshot.weather_risks
            for offset in range((risk.ends_at.date() - risk.starts_at.date()).days + 1)
        }
        days = tuple(item.date for item in snapshot.planner_context.days)
        remaining_slots = {day: ["09:00", "14:00"] for day in days}
        proposals: list[PlannerPlacementProposal] = []
        for candidate in snapshot.poi_candidates:
            rainy = tuple(day for day in days if day in weather_dates)
            dry = tuple(day for day in days if day not in weather_dates)
            preferred_days = (
                (*rainy, *dry)
                if candidate.environment == ActivityEnvironment.INDOOR
                else (*dry, *rainy)
            )
            selected_day = next(day for day in preferred_days if remaining_slots[day])
            proposals.append(
                PlannerPlacementProposal(
                    candidate_id=candidate.candidate_id,
                    day_number=days.index(selected_day) + 1,
                    start_time=remaining_slots[selected_day].pop(0),
                    reason="单 Agent 基于同一冻结工具快照完成候选取舍、天气规避与排程。",
                )
            )
        return PlannerModelResponse(
            proposal=PlannerProposalBatch(items=tuple(proposals)),
            model=COMPARISON_FIXTURE_MODEL,
            latency_ms=25,
            usage=ModelTokenUsage(
                prompt_tokens=200,
                completion_tokens=40,
                total_tokens=240,
            ),
        )

    def select_stay(self, snapshot: ComparisonToolSnapshot) -> str:
        if not snapshot.stay_candidates:
            raise ComparisonEvaluationError("Single Agent requires stay tool candidates")
        return snapshot.stay_candidates[0].candidate_id


class _ComparisonProductPlanModel:
    def __init__(self, policy: ComparisonFixtureSingleAgentPolicy) -> None:
        self._policy = policy

    def propose(self, materials: PlanningMaterialBundle) -> PlannerModelResponse:
        return self._policy.propose(build_comparison_tool_snapshot(materials))


class _ComparisonFixtureProductPipeline:
    """Replays healthy fixture artifacts through the real ProductRepairExecutor."""

    def __init__(
        self,
        healthy_materials: PlanningMaterialBundle,
        policy: ComparisonFixtureSingleAgentPolicy,
    ) -> None:
        self._healthy_materials = healthy_materials
        self._plan_model = _ComparisonProductPlanModel(policy)

    async def rerun_explore(self, context: PlannerContext) -> ExploreAgentResult:
        del context
        branch = next(
            item
            for item in self._healthy_materials.specialist_result.branches
            if item.specialist == SpecialistName.EXPLORE
        )
        if branch.explore_result is None:
            raise ComparisonEvaluationError("comparison fixture has no Explore result")
        return branch.explore_result

    async def rerun_stay(self, context: PlannerContext) -> StayAgentResult:
        del context
        branch = next(
            item
            for item in self._healthy_materials.specialist_result.branches
            if item.specialist == SpecialistName.STAY
        )
        if branch.stay_result is None:
            raise ComparisonEvaluationError("comparison fixture has no Stay result")
        return branch.stay_result

    async def build_materials(
        self,
        specialist_result: SpecialistFanoutResult,
    ) -> PlanningMaterialBundle:
        return PlanningMaterialBundle.model_validate(
            self._healthy_materials.model_copy(
                update={"specialist_result": specialist_result}
            ).model_dump(mode="python")
        )

    def run_plan(
        self,
        request: TripRequest,
        materials: PlanningMaterialBundle,
    ) -> PlanAgentRunResult:
        return run_plan_agent(request, materials, self._plan_model)

    def build_opening_hours(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        data_mode: DataMode,
    ) -> OpeningHoursEvidenceBundle:
        if data_mode != DataMode.FIXTURE:
            raise ComparisonEvaluationError("comparison pipeline is fixture-only")
        return _opening_hours(request, plan, ComparisonScenario.CLEAN)


class _ComparisonFixture:
    def __init__(
        self,
        case: ComparisonEvalCase,
        request: TripRequest,
        healthy_materials: PlanningMaterialBundle,
        tool_snapshot: ComparisonToolSnapshot,
    ) -> None:
        self.case = case
        self.request = request
        self.healthy_materials = healthy_materials
        self.tool_snapshot = tool_snapshot
        self.fault_fixture_sha256 = _sha256(
            {
                "case": case.model_dump(mode="json"),
                "tool_snapshot_sha256": tool_snapshot.snapshot_sha256,
                "fault_injection_version": "comparison-fault-injection-v1",
            }
        )


def _request_scenario(scenario: ComparisonScenario) -> HardConstraintScenario:
    if scenario in {
        ComparisonScenario.HARD_BUDGET_INCOMPLETE,
        ComparisonScenario.BUDGET_FLOOR_EXCEEDED,
    }:
        return HardConstraintScenario.HARD_BUDGET
    if scenario == ComparisonScenario.SCHEDULED_AVOID:
        return HardConstraintScenario.SCHEDULED_AVOID
    if scenario == ComparisonScenario.MISSING_MUST_VISIT:
        return HardConstraintScenario.MISSING_MUST_VISIT
    return HardConstraintScenario.PRESERVE


def _material_mutation(scenario: ComparisonScenario) -> HardMaterialMutation:
    return {
        ComparisonScenario.POI_CROSS_CITY: HardMaterialMutation.POI_CROSS_CITY,
        ComparisonScenario.STAY_CROSS_CITY: HardMaterialMutation.STAY_CROSS_CITY,
    }.get(scenario, HardMaterialMutation.NONE)


def _plan_mutation(scenario: ComparisonScenario) -> HardPlanMutation:
    return {
        ComparisonScenario.MISSING_ROUTE: HardPlanMutation.MISSING_ROUTE,
        ComparisonScenario.TIGHT_TRANSFER: HardPlanMutation.TIGHT_TRANSFER,
        ComparisonScenario.CANDIDATE_SOURCE_MISMATCH: (HardPlanMutation.CANDIDATE_SOURCE_MISMATCH),
        ComparisonScenario.ROUTE_SOURCE_MISMATCH: HardPlanMutation.ROUTE_SOURCE_MISMATCH,
    }.get(scenario, HardPlanMutation.NONE)


def _opening_hours(
    request: TripRequest,
    plan: TripPlan,
    scenario: ComparisonScenario,
) -> OpeningHoursEvidenceBundle:
    grounded = tuple(
        (day, item)
        for day in plan.days
        for item in day.items
        if item.kind in {ActivityKind.ATTRACTION, ActivityKind.MEAL}
        and item.candidate_id is not None
    )
    target_id = grounded[-1][1].item_id if grounded else None
    items: list[OpeningHoursEvidence] = []
    for day, item in grounded:
        if scenario == ComparisonScenario.MISSING_OPENING_EVIDENCE and item.item_id == target_id:
            continue
        duration = item.end_at - item.start_at
        opens_at = datetime.combine(day.date, time(8), tzinfo=item.start_at.tzinfo)
        closes_at = datetime.combine(day.date, time(22), tzinfo=item.start_at.tzinfo)
        if (
            scenario
            in {
                ComparisonScenario.OUTSIDE_OPENING_WINDOW,
                ComparisonScenario.OPENING_WINDOW_NO_FIT,
            }
            and item.item_id == target_id
        ):
            opens_at = item.end_at + timedelta(minutes=30)
            closes_at = opens_at + (
                timedelta(minutes=30)
                if scenario == ComparisonScenario.OPENING_WINDOW_NO_FIT
                else duration + timedelta(hours=1)
            )
        assert item.candidate_id is not None
        items.append(
            OpeningHoursEvidence(
                evidence_id=f"comparison-opening-{item.item_id}",
                candidate_id=item.candidate_id,
                service_date=day.date,
                opens_at=opens_at,
                closes_at=closes_at,
                source=SourceReference(
                    provider="comparison-opening-fixture",
                    provider_id=f"opening-{item.item_id}",
                    data_mode=DataMode.FIXTURE,
                    retrieved_at=datetime(2026, 8, 23, tzinfo=UTC),
                    raw_response_sha256=_sha256(
                        {
                            "item_id": item.item_id,
                            "opens_at": opens_at.isoformat(),
                            "closes_at": closes_at.isoformat(),
                            "scenario": scenario.value,
                        }
                    ),
                ),
            )
        )
    return OpeningHoursEvidenceBundle(
        request_id=request.request_id,
        data_mode=DataMode.FIXTURE,
        items=tuple(items),
    )


def build_clean_comparison_opening_hours(
    request: TripRequest,
    plan: TripPlan,
) -> OpeningHoursEvidenceBundle:
    """Build the frozen clean opening-evidence view used by comparison runners."""

    return _opening_hours(request, plan, ComparisonScenario.CLEAN)


def _inject_budget_floor(
    case: ComparisonEvalCase,
    request: TripRequest,
    plan: TripPlan,
) -> TripPlan:
    if case.scenario != ComparisonScenario.BUDGET_FLOOR_EXCEEDED:
        return plan
    if request.budget is None:
        raise ComparisonEvaluationError("budget-floor fixture requires a budget")
    category = request.budget.included_categories[0]
    amount = request.budget.total_limit + Decimal("100.00")
    cost = CostItem(
        cost_item_id=f"comparison-cost-{case.case_id}",
        category=BudgetCategory(category),
        description="comparison fixture deterministic floor",
        quantity=Decimal("1.00"),
        unit_price=MoneyRange(minimum=amount, maximum=amount),
        source=SourceReference(
            provider="comparison-cost-fixture",
            provider_id=f"cost-{case.case_id}",
            data_mode=DataMode.FIXTURE,
            retrieved_at=datetime(2026, 8, 23, tzinfo=UTC),
            raw_response_sha256=_sha256({"case_id": case.case_id, "amount": str(amount)}),
        ),
        is_estimate=False,
    )
    return TripPlan.model_validate(
        plan.model_copy(update={"cost_items": (cost,)}).model_dump(mode="python")
    )


async def _build_fixture(
    case: ComparisonEvalCase,
    source: PlanAgentEvalCase,
) -> _ComparisonFixture:
    base_materials = await build_plan_agent_materials(source)
    request = _mutate_request(
        source.request,
        base_materials,
        _request_scenario(case.scenario),
    )
    if request != source.request:
        source = source.model_copy(update={"request": request})
        base_materials = await build_plan_agent_materials(source)
    return _ComparisonFixture(
        case,
        request,
        base_materials,
        build_comparison_tool_snapshot(base_materials),
    )


def _initial_provider_calls(materials: PlanningMaterialBundle) -> int:
    return (
        materials.specialist_result.total_provider_call_count
        + materials.route_matrix.provider_call_count
    )


def _product_usage(
    materials: PlanningMaterialBundle,
    plan_result: PlanAgentRunResult,
) -> tuple[bool, int | None]:
    usages = tuple(
        usage for branch in materials.specialist_result.branches for usage in branch.model_usages
    ) + ((plan_result.usage,) if plan_result.usage is not None else ())
    model_calls = materials.specialist_result.total_model_call_count + plan_result.model_call_count
    complete = len(usages) == model_calls
    return complete, sum(item.total_tokens for item in usages) if complete else None


def _single_plan(
    fixture: _ComparisonFixture,
    policy: ComparisonFixtureSingleAgentPolicy,
) -> tuple[PlanAgentRunResult, str]:
    selected_stay_id = policy.select_stay(fixture.tool_snapshot)
    if selected_stay_id != fixture.tool_snapshot.route_anchor_candidate_id:
        raise ComparisonEvaluationError(
            "fixture route matrix does not cover the Single Agent stay selection"
        )
    response = policy.propose(fixture.tool_snapshot)
    return (
        normalize_plan_response(
            fixture.request,
            fixture.healthy_materials,
            response,
        ),
        selected_stay_id,
    )


def _product_plan(
    fixture: _ComparisonFixture,
    policy: ComparisonFixtureSingleAgentPolicy,
) -> tuple[PlanAgentRunResult, str]:
    primary_stay = fixture.healthy_materials.shortlist.primary_stay
    if primary_stay is None:
        raise ComparisonEvaluationError("usable Product fixture requires a stay selection")
    return (
        run_plan_agent(
            fixture.request,
            fixture.healthy_materials,
            _ComparisonProductPlanModel(policy),
        ),
        primary_stay.candidate_id,
    )


def _inject_plan_stage(
    fixture: _ComparisonFixture,
    plan: TripPlan,
) -> tuple[PlanningMaterialBundle, TripPlan, OpeningHoursEvidenceBundle]:
    plan = _inject_budget_floor(fixture.case, fixture.request, plan)
    plan = _mutate_plan(plan, _plan_mutation(fixture.case.scenario))
    materials = _mutate_materials(
        fixture.healthy_materials,
        _material_mutation(fixture.case.scenario),
    )
    opening = _opening_hours(fixture.request, plan, fixture.case.scenario)
    return materials, plan, opening


async def _run_arm(
    fixture: _ComparisonFixture,
    arm: ComparisonArm,
    policy: ComparisonFixtureSingleAgentPolicy,
) -> tuple[ComparisonRunOutput, PlanningMaterialBundle]:
    materials = fixture.healthy_materials
    product_arm = arm != ComparisonArm.SINGLE_AGENT_TOOLS
    base_model_calls = materials.specialist_result.total_model_call_count if product_arm else 0
    provider_calls = _initial_provider_calls(materials)
    if not planning_materials_support_draft(materials):
        usage_complete = not product_arm or base_model_calls == 0
        return (
            ComparisonRunOutput(
                case_id=fixture.case.case_id,
                arm=arm,
                outcome=ComparisonOutcome.BLOCKED_BEFORE_PLAN,
                tool_snapshot_sha256=fixture.tool_snapshot.snapshot_sha256,
                fault_fixture_sha256=fixture.fault_fixture_sha256,
                selected_stay_candidate_id=None,
                plan=None,
                initial_validation=None,
                final_validation=None,
                model_call_count=base_model_calls,
                provider_call_count=provider_calls,
                token_usage_complete=usage_complete,
                total_tokens=0 if usage_complete else None,
            ),
            materials,
        )

    plan_result, selected_stay_id = (
        _single_plan(fixture, policy)
        if arm == ComparisonArm.SINGLE_AGENT_TOOLS
        else _product_plan(fixture, policy)
    )
    if plan_result.status != PlanAgentRunStatus.PLANNED or plan_result.plan is None:
        raise ComparisonEvaluationError("usable comparison fixture did not produce a plan")
    initial_materials, initial_plan, opening = _inject_plan_stage(
        fixture,
        plan_result.plan,
    )
    initial_validation = validate_hard_trip_plan(
        fixture.request,
        initial_plan,
        initial_materials,
        opening,
    )
    if arm == ComparisonArm.SINGLE_AGENT_TOOLS:
        usage_complete = plan_result.usage is not None
        total_tokens = plan_result.usage.total_tokens if plan_result.usage is not None else None
        model_calls = plan_result.model_call_count
    else:
        usage_complete, total_tokens = _product_usage(materials, plan_result)
        model_calls = base_model_calls + plan_result.model_call_count

    repair: RepairRouterResult | None = None
    final_materials = initial_materials
    final_plan = initial_plan
    final_validation = initial_validation
    outcome = (
        ComparisonOutcome.FINALIZABLE_WITHOUT_REPAIR
        if initial_validation.can_finalize
        else ComparisonOutcome.UNRESOLVED
    )
    if arm == ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR and not initial_validation.can_finalize:
        pipeline = _ComparisonFixtureProductPipeline(materials, policy)
        repair = await run_repair_router(
            fixture.request,
            initial_plan,
            initial_materials,
            opening,
            ProductRepairExecutor(pipeline),
        )
        final_materials = repair.final_materials
        final_plan = repair.final_plan
        final_validation = repair.final_report
        outcome = {
            RepairOutcome.ALREADY_FINALIZABLE: ComparisonOutcome.FINALIZABLE_WITHOUT_REPAIR,
            RepairOutcome.REPAIRED: ComparisonOutcome.REPAIRED,
            RepairOutcome.WAITING_FOR_USER: ComparisonOutcome.WAITING_FOR_USER,
            RepairOutcome.UNRESOLVED: ComparisonOutcome.UNRESOLVED,
        }[repair.outcome]
        model_calls += repair.total_model_call_count
        provider_calls += repair.total_provider_call_count
        if repair.total_model_call_count:
            usage_complete = False
            total_tokens = None

    return (
        ComparisonRunOutput(
            case_id=fixture.case.case_id,
            arm=arm,
            outcome=outcome,
            tool_snapshot_sha256=fixture.tool_snapshot.snapshot_sha256,
            fault_fixture_sha256=fixture.fault_fixture_sha256,
            selected_stay_candidate_id=selected_stay_id,
            plan=final_plan,
            initial_validation=initial_validation,
            final_validation=final_validation,
            repair=repair,
            model_call_count=model_calls,
            provider_call_count=provider_calls,
            token_usage_complete=usage_complete,
            total_tokens=total_tokens,
        ),
        final_materials,
    )


def _error_codes(report: PlanValidationReport | None) -> tuple[str, ...]:
    if report is None:
        return ()
    return tuple(item.rule_code for item in report.issues if item.severity == IssueSeverity.ERROR)


def _plan_sha256(plan: TripPlan | None) -> str | None:
    return _sha256(plan.model_dump(mode="json")) if plan is not None else None


def _evidence_counts(
    plan: TripPlan | None,
    materials: PlanningMaterialBundle,
) -> tuple[int, int, int, int]:
    if plan is None:
        return (0, 0, 0, 0)
    scheduled = tuple(
        item
        for day in plan.days
        for item in day.items
        if item.kind in {ActivityKind.ATTRACTION, ActivityKind.MEAL}
    )
    candidates = {item.candidate_id: item for item in materials.shortlist.poi_candidates}
    grounded = tuple(
        item
        for item in scheduled
        if item.candidate_id in candidates
        and item.title == candidates[item.candidate_id].name
        and item.source == candidates[item.candidate_id].source
    )
    traceable = tuple(
        item
        for item in grounded
        if item.source is not None
        and item.source.provider_id is not None
        and item.source.data_mode == DataMode.FIXTURE
    )
    routes = tuple(edge.route for edge in materials.route_matrix.edges if edge.route is not None)
    route_backed = tuple(
        item
        for item in traceable
        if item.route_from_previous is not None and item.route_from_previous in routes
    )
    return (len(scheduled), len(grounded), len(traceable), len(route_backed))


def _frozen_expectation_match(
    case: ComparisonEvalCase,
    output: ComparisonRunOutput,
) -> bool:
    expected = case.expected
    repair_actions = (
        tuple(item.repair_action for item in output.repair.attempts)
        if output.repair is not None
        else ()
    )
    stop_reason = output.repair.stop_reason if output.repair is not None else None
    final_can_finalize = (
        output.final_validation.can_finalize if output.final_validation is not None else None
    )
    return (
        _error_codes(output.initial_validation) == expected.initial_error_codes
        and output.outcome == expected.full_outcome
        and final_can_finalize == expected.final_can_finalize
        and repair_actions == expected.repair_actions
        and stop_reason == expected.stop_reason
    )


def _case_result(
    case: ComparisonEvalCase,
    output: ComparisonRunOutput,
    materials: PlanningMaterialBundle,
) -> ComparisonArmCaseResult:
    full_arm = output.arm == ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR
    expectation_match = _frozen_expectation_match(case, output) if full_arm else None
    repair_actions = (
        tuple(item.repair_action for item in output.repair.attempts)
        if output.repair is not None
        else ()
    )
    stop_reason = output.repair.stop_reason if output.repair is not None else None
    scheduled, grounded, traceable, route_backed = _evidence_counts(output.plan, materials)
    checks = (
        EvaluationCheck(code="workflow_completed", passed=True),
        EvaluationCheck(
            code="common_tool_snapshot_recorded",
            passed=len(output.tool_snapshot_sha256) == 64,
        ),
        EvaluationCheck(
            code="common_fault_fixture_recorded",
            passed=len(output.fault_fixture_sha256) == 64,
        ),
        EvaluationCheck(
            code="shared_post_run_evaluator_applied",
            passed=(
                output.outcome == ComparisonOutcome.BLOCKED_BEFORE_PLAN
                or output.final_validation is not None
            ),
        ),
        EvaluationCheck(
            code="full_trip_plan_contract_preserved",
            passed=(
                output.outcome == ComparisonOutcome.BLOCKED_BEFORE_PLAN or output.plan is not None
            ),
        ),
        EvaluationCheck(
            code="repair_boundary_preserved",
            passed=(output.repair is not None)
            == (
                output.arm == ComparisonArm.PRODUCT_GRAPH_BOUNDED_REPAIR
                and output.outcome
                in {
                    ComparisonOutcome.REPAIRED,
                    ComparisonOutcome.WAITING_FOR_USER,
                    ComparisonOutcome.UNRESOLVED,
                }
            ),
        ),
        EvaluationCheck(
            code="frozen_full_outcome_matches",
            passed=expectation_match is not False,
        ),
    )
    return ComparisonArmCaseResult(
        case_id=case.case_id,
        arm=output.arm,
        tier=case.tier,
        scenario=case.scenario,
        protocol_passed=all(item.passed for item in checks),
        frozen_expectation_match=expectation_match,
        outcome=output.outcome,
        tool_snapshot_sha256=output.tool_snapshot_sha256,
        fault_fixture_sha256=output.fault_fixture_sha256,
        selected_stay_candidate_id=output.selected_stay_candidate_id,
        plan_sha256=_plan_sha256(output.plan),
        initial_error_codes=_error_codes(output.initial_validation),
        final_error_codes=_error_codes(output.final_validation),
        final_can_finalize=(
            output.final_validation.can_finalize if output.final_validation is not None else None
        ),
        repair_actions=repair_actions,
        repair_stop_reason=stop_reason,
        scheduled_candidate_count=scheduled,
        grounded_candidate_count=grounded,
        traceable_candidate_count=traceable,
        route_backed_candidate_count=route_backed,
        model_call_count=output.model_call_count,
        provider_call_count=output.provider_call_count,
        token_usage_complete=output.token_usage_complete,
        total_tokens=output.total_tokens,
        latency_ms=output.latency_ms,
        checks=checks,
    )


def _arm_summary(
    arm: ComparisonArm,
    results: tuple[ComparisonArmCaseResult, ...],
) -> ComparisonArmSummary:
    eligible = sum(item.final_can_finalize is not None for item in results)
    finalizable = sum(item.final_can_finalize is True for item in results)
    scheduled = sum(item.scheduled_candidate_count for item in results)
    token_complete = sum(item.token_usage_complete for item in results)
    return ComparisonArmSummary(
        arm=arm,
        protocol_passed_case_count=sum(item.protocol_passed for item in results),
        eligible_case_count=eligible,
        blocked_case_count=sum(
            item.outcome == ComparisonOutcome.BLOCKED_BEFORE_PLAN for item in results
        ),
        finalizable_case_count=finalizable,
        finalization_rate=expected_rate(finalizable, eligible),
        finalizable_without_repair_case_count=sum(
            item.outcome == ComparisonOutcome.FINALIZABLE_WITHOUT_REPAIR for item in results
        ),
        repaired_case_count=sum(item.outcome == ComparisonOutcome.REPAIRED for item in results),
        waiting_for_user_case_count=sum(
            item.outcome == ComparisonOutcome.WAITING_FOR_USER for item in results
        ),
        unresolved_case_count=sum(item.outcome == ComparisonOutcome.UNRESOLVED for item in results),
        scheduled_candidate_count=scheduled,
        grounding_rate=expected_rate(
            sum(item.grounded_candidate_count for item in results), scheduled
        ),
        source_traceability_rate=expected_rate(
            sum(item.traceable_candidate_count for item in results), scheduled
        ),
        route_lineage_rate=expected_rate(
            sum(item.route_backed_candidate_count for item in results), scheduled
        ),
        model_call_count=sum(item.model_call_count for item in results),
        provider_call_count=sum(item.provider_call_count for item in results),
        token_complete_case_count=token_complete,
        total_tokens=(
            sum(item.total_tokens or 0 for item in results)
            if token_complete == len(results)
            else None
        ),
        p50_latency_ms=None,
        p95_latency_ms=None,
        results=results,
    )


def _paired_delta(
    left: ComparisonArmSummary,
    right: ComparisonArmSummary,
) -> ComparisonPairedDelta:
    left_by_id = {item.case_id: item for item in left.results}
    right_by_id = {item.case_id: item for item in right.results}
    shared = tuple(
        case_id
        for case_id in left_by_id
        if left_by_id[case_id].final_can_finalize is not None
        and right_by_id[case_id].final_can_finalize is not None
    )
    improved = sum(
        left_by_id[item].final_can_finalize is False
        and right_by_id[item].final_can_finalize is True
        for item in shared
    )
    worsened = sum(
        left_by_id[item].final_can_finalize is True
        and right_by_id[item].final_can_finalize is False
        for item in shared
    )
    return ComparisonPairedDelta(
        from_arm=left.arm,
        to_arm=right.arm,
        shared_eligible_case_count=len(shared),
        improved_case_count=improved,
        worsened_case_count=worsened,
        unchanged_case_count=len(shared) - improved - worsened,
        finalization_rate_delta=(right.finalization_rate - left.finalization_rate).quantize(
            Decimal("0.0001")
        ),
    )


async def evaluate_system_comparison_fixture() -> SystemComparisonReport:
    suite = load_comparison_suite()
    source_by_id = {item.case_id: item for item in load_plan_agent_suite().cases}
    policy = ComparisonFixtureSingleAgentPolicy()
    by_arm: dict[ComparisonArm, list[ComparisonArmCaseResult]] = {arm: [] for arm in suite.arms}
    for case in suite.cases:
        try:
            source = source_by_id[case.source_plan_case_id]
        except KeyError as error:
            raise ComparisonEvaluationError(
                f"unknown comparison source Plan Agent case: {error.args[0]}"
            ) from error
        fixture = await _build_fixture(case, source)
        for arm in suite.arms:
            output, final_materials = await _run_arm(fixture, arm, policy)
            by_arm[arm].append(_case_result(case, output, final_materials))
    summaries = tuple(_arm_summary(arm, tuple(by_arm[arm])) for arm in suite.arms)
    return SystemComparisonReport(
        dataset_sha256=comparison_dataset_sha256(suite),
        full_expectation_match_count=sum(
            item.frozen_expectation_match is True for item in summaries[-1].results
        ),
        arms=summaries,
        paired_deltas=(
            _paired_delta(summaries[0], summaries[1]),
            _paired_delta(summaries[1], summaries[2]),
            _paired_delta(summaries[0], summaries[2]),
        ),
        limitations=(
            "30 条案例是开发回归与控制路径故障注入, 不是未触碰 holdout 或真实用户成功率。",
            "Single Agent 与 Product arms 共享冻结工具快照和同一排程策略; "
            "fixture 不能证明 Specialist 模型质量提升。",
            "故障在统一草案边界注入, 报告测量 Hard Validator 与有界 Repair 的恢复能力, "
            "不测开放式规划偏好质量。",
            "fixture specialist 未记录完整 token usage, "
            "因此 Product arms 不发布 token 总量或延迟分位数。",
            "Single Agent 能查看全部住宿候选并显式选择锚点; "
            "fixture 路线矩阵只覆盖最终锚点, 不构成酒店排序质量评测。",
            "本报告没有调用 DeepSeek、高德或 LangSmith, live 对照仍需显式 opt-in 和重复运行。",
            "评测直接调用生产组件边界而不启动 HTTP/checkpoint Graph; 产品图另有 full-stack E2E。",
        ),
    )


__all__ = [
    "COMPARISON_FIXTURE_MODEL",
    "ComparisonFixtureSingleAgentPolicy",
    "build_clean_comparison_opening_hours",
    "build_comparison_tool_snapshot",
    "evaluate_system_comparison_fixture",
]
