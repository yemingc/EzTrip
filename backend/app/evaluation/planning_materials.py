import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.agents.contracts import (
    ExploreCandidateSelectionProposal,
    ExploreEvidenceKind,
    ExploreEvidenceReference,
    ExploreSelectionModelResponse,
    ExploreSelectionProposalBatch,
    StayCandidateSelectionProposal,
    StayEvidenceKind,
    StayEvidenceReference,
    StaySelectionModelResponse,
    StaySelectionProposalBatch,
)
from app.domain.provider import ProviderErrorCategory, ProviderFailure
from app.domain.request import TripRequest
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import RouteLeg, RouteMode
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.planning_materials_contracts import (
    PlanningMaterialBaselineReport,
    PlanningMaterialCaseResult,
    PlanningMaterialEvalCase,
    PlanningMaterialEvalSuite,
    RouteFailureInjection,
)
from app.evaluation.specialist_fanout import (
    FixtureExploreModel,
    FixtureStayModel,
    build_specialist_scenario_provider,
    load_specialist_fanout_suite,
    specialist_fanout_dataset_sha256,
)
from app.planning.material_builder import build_planning_material_bundle
from app.planning.material_contracts import BudgetAllocationStatus, RouteEdgeStatus
from app.planning.specialist_fanout import run_specialist_fanout
from app.providers.errors import ProviderRequestError
from app.providers.ports import RouteRequest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PLANNING_MATERIAL_SUITE_PATH = (
    REPOSITORY_ROOT / "evals" / "cases" / "planning-materials" / "suite.v1.json"
)


class PlanningMaterialEvaluationError(RuntimeError):
    """Raised when planning-material fixtures contradict their versioned contracts."""


class PlanningMaterialFixtureExploreModel(FixtureExploreModel):
    def select_candidates(
        self,
        context: Any,
        queries: Any,
        observations: Any,
    ) -> ExploreSelectionModelResponse:
        del context, queries
        return ExploreSelectionModelResponse(
            proposal=ExploreSelectionProposalBatch(
                items=tuple(
                    ExploreCandidateSelectionProposal(
                        candidate_id=observation.candidate.candidate_id,
                        rank=index,
                        reason="固定夹具候选用于路线矩阵评测。",
                        evidence=(
                            ExploreEvidenceReference(
                                kind=ExploreEvidenceKind.CATEGORY,
                                value=observation.candidate.categories[0],
                            ),
                        ),
                    )
                    for index, observation in enumerate(observations, start=1)
                )
            ),
            model="fixture-explore-planning-materials-model",
            latency_ms=20,
        )


class PlanningMaterialFixtureStayModel(FixtureStayModel):
    def select_candidates(
        self,
        context: Any,
        queries: Any,
        observations: Any,
    ) -> StaySelectionModelResponse:
        del context, queries
        return StaySelectionModelResponse(
            proposal=StaySelectionProposalBatch(
                items=tuple(
                    StayCandidateSelectionProposal(
                        candidate_id=observation.candidate.candidate_id,
                        rank=index,
                        reason="固定夹具候选用于住宿锚点评测。",
                        evidence=(
                            StayEvidenceReference(
                                kind=StayEvidenceKind.AREA_NAME,
                                value=observation.candidate.area_name,
                            ),
                        ),
                    )
                    for index, observation in enumerate(observations, start=1)
                )
            ),
            model="fixture-stay-planning-materials-model",
            latency_ms=21,
        )


class PlanningMaterialRouteProvider:
    """Traceable route fixture with one optional typed timeout."""

    def __init__(self, failure: RouteFailureInjection) -> None:
        self.failure = failure
        self.calls: list[RouteRequest] = []
        self.active = 0
        self.peak_active = 0

    async def get_route(self, request: RouteRequest) -> RouteLeg:
        call_index = len(self.calls)
        self.calls.append(request)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if self.failure == RouteFailureInjection.ONE_TIMEOUT and call_index == 0:
                raise ProviderRequestError(
                    ProviderFailure(
                        provider="planning-material-route-fixture",
                        operation="get_route",
                        category=ProviderErrorCategory.TIMEOUT,
                        message="injected route timeout",
                        retryable=True,
                    )
                )
            origin_id = request.origin.candidate_id or "origin"
            destination_id = request.destination.candidate_id or "destination"
            material = f"{origin_id}|{destination_id}|transit"
            digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
            distance = round(
                (
                    abs(request.origin.location.latitude - request.destination.location.latitude)
                    + abs(
                        request.origin.location.longitude - request.destination.location.longitude
                    )
                )
                * 100_000
            )
            return RouteLeg(
                route_leg_id=f"planning-route-{digest[:16]}",
                origin=request.origin,
                destination=request.destination,
                mode=RouteMode.TRANSIT,
                distance_meters=max(distance, 100),
                duration_minutes=max(round(distance / 350), 1),
                source=SourceReference(
                    provider="planning-material-route-fixture",
                    provider_id=f"route-{digest[:12]}",
                    data_mode=DataMode.FIXTURE,
                    retrieved_at=datetime(2026, 8, 21, tzinfo=UTC),
                    raw_response_sha256=digest,
                ),
            )
        finally:
            self.active -= 1


def load_planning_material_suite(
    suite_path: Path = PLANNING_MATERIAL_SUITE_PATH,
) -> PlanningMaterialEvalSuite:
    return PlanningMaterialEvalSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))


def planning_material_dataset_sha256(suite: PlanningMaterialEvalSuite) -> str:
    specialist_suite = load_specialist_fanout_suite()
    canonical = json.dumps(
        {
            "suite": suite.model_dump(mode="json"),
            "specialist_fanout_dataset_sha256": specialist_fanout_dataset_sha256(specialist_suite),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_for_case(case: PlanningMaterialEvalCase) -> tuple[TripRequest, Any]:
    specialist_case = next(
        (
            item
            for item in load_specialist_fanout_suite().cases
            if item.case_id == case.specialist_case_id
        ),
        None,
    )
    if specialist_case is None:
        raise PlanningMaterialEvaluationError(
            f"unknown specialist fan-out fixture: {case.specialist_case_id}"
        )
    payload = specialist_case.request.model_dump(mode="json")
    if case.request_budget is not None:
        payload["budget"] = case.request_budget.model_dump(mode="json")
    elif not case.preserve_source_budget:
        payload["budget"] = None
    return TripRequest.model_validate(payload), specialist_case


def _stable_error_code(error: Exception) -> str:
    material = f"{error.__class__.__module__}|{error.__class__.__qualname__}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"planning-material-error-{digest}"


def _failed_result(case: PlanningMaterialEvalCase, error: Exception) -> PlanningMaterialCaseResult:
    return PlanningMaterialCaseResult(
        case_id=case.case_id,
        expected_material_status=case.expected.material_status,
        passed=False,
        expected_edge_count=case.expected.expected_edge_count,
        actual_edge_count=0,
        failed_edge_count=0,
        typed_route_failure_count=0,
        route_provider_call_count=0,
        route_provider_peak=0,
        source_traceability_passed=False,
        budget_sum_exact=False,
        budget_target_total=Decimal("0"),
        error_code=_stable_error_code(error),
        checks=(EvaluationCheck(code="workflow_completed", passed=False),),
    )


async def evaluate_planning_material_case(
    case: PlanningMaterialEvalCase,
) -> PlanningMaterialCaseResult:
    route_provider = PlanningMaterialRouteProvider(case.route_failure)
    try:
        request, specialist_case = _request_for_case(case)
        specialist_result = await run_specialist_fanout(
            request,
            build_specialist_scenario_provider(specialist_case),
            PlanningMaterialFixtureExploreModel(),
            PlanningMaterialFixtureStayModel(),
            data_mode=DataMode.FIXTURE,
        )
        bundle = await build_planning_material_bundle(specialist_result, route_provider)
    except Exception as error:
        return _failed_result(case, error)

    matrix = bundle.route_matrix
    allocation = bundle.budget_allocation
    typed_failures = sum(
        item.status == RouteEdgeStatus.FAILED
        and item.failure is not None
        and item.failure.provider_category == ProviderErrorCategory.TIMEOUT
        and item.failure.retryable
        for item in matrix.edges
    )
    successful_routes = tuple(item.route for item in matrix.edges if item.route is not None)
    source_traceability = bool(successful_routes) and all(
        item.source.data_mode == DataMode.FIXTURE
        and item.source.provider
        and item.source.provider_id
        and item.source.raw_response_sha256
        for item in successful_routes
    )
    budget_target_total = sum(
        (item.target_amount for item in allocation.allocations),
        start=Decimal("0"),
    )
    budget_sum_exact = (
        allocation.status == BudgetAllocationStatus.ALLOCATED
        and allocation.total_limit is not None
        and budget_target_total == allocation.total_limit
    )
    checks = (
        EvaluationCheck(code="workflow_completed", passed=True),
        EvaluationCheck(
            code="material_status_matches",
            passed=bundle.status == case.expected.material_status,
        ),
        EvaluationCheck(
            code="route_status_matches",
            passed=matrix.status == case.expected.route_status,
        ),
        EvaluationCheck(
            code="budget_status_matches",
            passed=allocation.status == case.expected.budget_status,
        ),
        EvaluationCheck(
            code="edge_count_matches",
            passed=len(matrix.edges) == case.expected.expected_edge_count,
        ),
        EvaluationCheck(
            code="failed_edge_count_matches",
            passed=matrix.failed_edge_count == case.expected.expected_failed_edge_count,
        ),
        EvaluationCheck(
            code="route_calls_match",
            passed=len(route_provider.calls) == case.expected.expected_route_provider_calls,
        ),
        EvaluationCheck(
            code="typed_route_failures",
            passed=typed_failures == matrix.failed_edge_count,
        ),
        EvaluationCheck(
            code="budget_sum_exact",
            passed=(
                budget_sum_exact
                if case.expected.budget_status == BudgetAllocationStatus.ALLOCATED
                else not allocation.allocations
            ),
        ),
        EvaluationCheck(
            code="blocked_route_uses_zero_calls",
            passed=(
                len(route_provider.calls) == 0
                if case.expected.route_status.value == "blocked"
                else True
            ),
        ),
        EvaluationCheck(
            code="route_concurrency_bounded",
            passed=route_provider.peak_active <= 4,
        ),
        EvaluationCheck(
            code="route_sources_traceable",
            passed=source_traceability or not successful_routes,
        ),
    )
    return PlanningMaterialCaseResult(
        case_id=case.case_id,
        expected_material_status=case.expected.material_status,
        actual_material_status=bundle.status,
        passed=all(item.passed for item in checks),
        expected_edge_count=case.expected.expected_edge_count,
        actual_edge_count=len(matrix.edges),
        failed_edge_count=matrix.failed_edge_count,
        typed_route_failure_count=typed_failures,
        route_provider_call_count=len(route_provider.calls),
        route_provider_peak=route_provider.peak_active,
        source_traceability_passed=source_traceability,
        budget_sum_exact=budget_sum_exact,
        budget_target_total=budget_target_total,
        checks=checks,
    )


async def evaluate_planning_material_suite(
    suite_path: Path = PLANNING_MATERIAL_SUITE_PATH,
) -> PlanningMaterialBaselineReport:
    suite = load_planning_material_suite(suite_path)
    results = tuple([await evaluate_planning_material_case(case) for case in suite.cases])
    passed = sum(item.passed for item in results)
    return PlanningMaterialBaselineReport(
        dataset_sha256=planning_material_dataset_sha256(suite),
        passed_case_count=passed,
        case_pass_rate=expected_rate(passed, len(results)),
        expected_edge_count=sum(item.expected_edge_count for item in results),
        actual_edge_count=sum(item.actual_edge_count for item in results),
        route_provider_call_count=sum(item.route_provider_call_count for item in results),
        typed_route_failure_count=sum(item.typed_route_failure_count for item in results),
        exact_budget_case_count=sum(item.budget_sum_exact for item in results),
        blocked_zero_route_call_case_count=sum(
            item.actual_material_status is not None
            and item.actual_material_status.value == "blocked"
            and item.route_provider_call_count == 0
            for item in results
        ),
        bounded_concurrency_case_count=sum(item.route_provider_peak <= 4 for item in results),
        source_traceability_case_count=sum(item.source_traceability_passed for item in results),
        results=results,
        limitations=(
            "路线使用可控 fixture, 不代表实时高德公交耗时、换乘或拥堵数据。",
            "预算分配是工程策略目标, 不是价格来源、报价或预算满足证明。",
            "候选沿用开发集标签, 本套件验证材料编排而非未见城市上的推荐质量。",
            "当前产物是最终行程生成前的确定性材料层, 尚未生成逐日 TripPlan。",
        ),
    )
