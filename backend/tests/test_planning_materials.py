import asyncio
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

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
from app.domain.candidates import StayPriceBasis
from app.domain.money import BudgetCategory, MoneyRange
from app.domain.provider import ProviderErrorCategory, ProviderFailure
from app.domain.request import BudgetConstraint, TripRequest
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import RouteLeg, RouteMode
from app.evaluation.specialist_fanout import (
    FixtureExploreModel,
    FixtureStayModel,
    build_specialist_scenario_provider,
    load_specialist_fanout_suite,
)
from app.planning.budget_estimate_contracts import BudgetComparisonStatus, BudgetEstimateMethod
from app.planning.budget_estimator import estimate_trip_budget
from app.planning.context_compiler import compile_planner_context
from app.planning.material_builder import (
    PlanningMaterialProtocolError,
    allocate_budget,
    build_planning_material_bundle,
    build_planning_shortlist,
    build_route_matrix,
)
from app.planning.material_contracts import (
    BudgetAllocationReason,
    BudgetAllocationStatus,
    BudgetQuantityBasis,
    PlanningDayCluster,
    PlanningMaterialIssueCode,
    PlanningMaterialStatus,
    PlanningShortlist,
    RouteEdgeStatus,
    RouteFailureCategory,
    RouteMatrixReason,
    RouteMatrixStatus,
)
from app.planning.specialist_contracts import SpecialistFanoutResult
from app.planning.specialist_fanout import run_specialist_fanout
from app.providers.errors import ProviderRequestError
from app.providers.ports import RouteRequest


class MultiSelectExploreModel(FixtureExploreModel):
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
                        reason="固定夹具候选用于路线矩阵覆盖。",
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
            model="fixture-explore-material-model",
            latency_ms=20,
        )


class MultiSelectStayModel(FixtureStayModel):
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
                        reason="固定夹具候选用于住宿锚点选择。",
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
            model="fixture-stay-material-model",
            latency_ms=21,
        )


class ScenarioRouteProvider:
    def __init__(
        self,
        *,
        failed_pairs: set[tuple[str, str]] | None = None,
        fail_all: bool = False,
        return_wrong_endpoint: bool = False,
    ) -> None:
        self.failed_pairs = failed_pairs or set()
        self.fail_all = fail_all
        self.return_wrong_endpoint = return_wrong_endpoint
        self.calls: list[RouteRequest] = []
        self.active = 0
        self.peak_active = 0

    async def get_route(self, request: RouteRequest) -> RouteLeg:
        self.calls.append(request)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await asyncio.sleep(0.01)
            pair = (
                request.origin.candidate_id or "origin",
                request.destination.candidate_id or "destination",
            )
            if self.fail_all or pair in self.failed_pairs:
                raise ProviderRequestError(
                    ProviderFailure(
                        provider="route-material-fixture",
                        operation="get_route",
                        category=ProviderErrorCategory.TIMEOUT,
                        message="injected route timeout",
                        retryable=True,
                    )
                )
            material = f"{pair[0]}|{pair[1]}|transit"
            digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
            destination = request.destination
            if self.return_wrong_endpoint:
                destination = request.destination.model_copy(
                    update={"candidate_id": "wrong-endpoint"}
                )
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
                route_leg_id=f"fixture-route-{digest[:16]}",
                origin=request.origin,
                destination=destination,
                mode=RouteMode.TRANSIT,
                distance_meters=max(distance, 100),
                duration_minutes=max(round(distance / 350), 1),
                source=SourceReference(
                    provider="route-material-fixture",
                    provider_id=f"fixture-{digest[:12]}",
                    data_mode=DataMode.FIXTURE,
                    retrieved_at=datetime(2026, 8, 21, tzinfo=UTC),
                    raw_response_sha256=digest,
                ),
            )
        finally:
            self.active -= 1


def fanout_case(case_id: str) -> Any:
    return next(item for item in load_specialist_fanout_suite().cases if item.case_id == case_id)


def request_with_budget(
    request: TripRequest,
    *,
    total_limit: str,
    categories: tuple[BudgetCategory, ...],
) -> TripRequest:
    payload = request.model_dump(mode="json")
    payload["budget"] = BudgetConstraint(
        total_limit=Decimal(total_limit),
        included_categories=categories,
    ).model_dump(mode="json")
    return TripRequest.model_validate(payload)


async def build_fanout(
    case_id: str = "specialist-fanout-complete-v1",
    *,
    budget: BudgetConstraint | None = None,
) -> SpecialistFanoutResult:
    case = fanout_case(case_id)
    request = case.request
    if budget is not None:
        request = request_with_budget(
            request,
            total_limit=str(budget.total_limit),
            categories=budget.included_categories,
        )
    return await run_specialist_fanout(
        request,
        build_specialist_scenario_provider(case),
        MultiSelectExploreModel(),
        MultiSelectStayModel(),
        data_mode=DataMode.FIXTURE,
    )


def test_complete_material_bundle_builds_directed_matrix_with_bounded_concurrency() -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout(
            budget=BudgetConstraint(
                total_limit=Decimal("3000.00"),
                included_categories=(
                    BudgetCategory.TRANSPORT,
                    BudgetCategory.FOOD,
                    BudgetCategory.ADMISSION,
                    BudgetCategory.ACTIVITY,
                ),
            )
        )
        provider = ScenarioRouteProvider()

        bundle = await build_planning_material_bundle(specialist_result, provider)

        assert bundle.status == PlanningMaterialStatus.READY
        assert bundle.issues == ()
        assert len(bundle.shortlist.poi_candidates) == 3
        assert bundle.shortlist.primary_stay is not None
        assert len(bundle.shortlist.omitted_stay_ids) == 2
        assert bundle.route_matrix.status == RouteMatrixStatus.COMPLETE
        assert bundle.route_matrix.expected_edge_count == 12
        assert bundle.route_matrix.succeeded_edge_count == 12
        assert bundle.route_matrix.failed_edge_count == 0
        assert bundle.route_matrix.provider_call_count == len(provider.calls) == 12
        assert provider.peak_active == 4
        assert all(item.status == RouteEdgeStatus.SUCCEEDED for item in bundle.route_matrix.edges)
        assert all(
            item.route is not None and item.route.source.data_mode == DataMode.FIXTURE
            for item in bundle.route_matrix.edges
        )
        actual_pairs = tuple(
            (item.origin_candidate_id, item.destination_candidate_id)
            for item in bundle.route_matrix.edges
        )
        assert len(actual_pairs) == len(set(actual_pairs)) == 12

        allocation = bundle.budget_allocation
        assert allocation.status == BudgetAllocationStatus.ALLOCATED
        assert [item.target_amount for item in allocation.allocations] == [
            Decimal("1000.00"),
            Decimal("1250.00"),
            Decimal("500.00"),
            Decimal("250.00"),
        ]
        assert sum(
            (item.target_amount for item in allocation.allocations),
            start=Decimal("0"),
        ) == Decimal("3000.00")

        estimate = bundle.budget_estimate
        assert estimate is not None
        assert estimate.total == MoneyRange(minimum="720.00", maximum="2880.00")
        assert estimate.per_traveler == MoneyRange(minimum="360.00", maximum="1440.00")
        assert estimate.per_day == MoneyRange(minimum="240.00", maximum="960.00")
        assert estimate.comparison_status == BudgetComparisonStatus.WITHIN_BUDGET
        assert [item.category for item in estimate.items] == [
            BudgetCategory.TRANSPORT,
            BudgetCategory.FOOD,
            BudgetCategory.ADMISSION,
            BudgetCategory.ACTIVITY,
        ]
        assert all(
            item.method == BudgetEstimateMethod.PLANNING_REFERENCE for item in estimate.items
        )

    asyncio.run(exercise())


def test_four_poi_shortlist_caps_directed_route_matrix_at_twenty_edges() -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout()
        base = build_planning_shortlist(specialist_result)
        fourth = base.poi_candidates[0].model_copy(
            update={
                "candidate_id": "fixture-beijing-fourth-poi",
                "name": "第四个路线矩阵夹具景点",
            }
        )
        shortlist = PlanningShortlist(
            activity_target_per_day=base.activity_target_per_day,
            poi_candidates=(*base.poi_candidates, fourth),
            day_clusters=(
                PlanningDayCluster(
                    day_number=base.day_clusters[0].day_number,
                    poi_candidate_ids=(
                        *base.day_clusters[0].poi_candidate_ids,
                        fourth.candidate_id,
                    ),
                ),
                *base.day_clusters[1:],
            ),
            primary_stay=base.primary_stay,
            omitted_stay_ids=base.omitted_stay_ids,
        )
        provider = ScenarioRouteProvider()

        matrix = await build_route_matrix(specialist_result, shortlist, provider)

        assert matrix.status == RouteMatrixStatus.COMPLETE
        assert matrix.expected_edge_count == len(matrix.edges) == 20
        assert matrix.provider_call_count == len(provider.calls) == 20
        assert provider.peak_active == 4

    asyncio.run(exercise())


def test_all_category_budget_uses_auditable_weights_and_quantity_bases() -> None:
    case = fanout_case("specialist-fanout-complete-v1")
    request = request_with_budget(
        case.request,
        total_limit="300.00",
        categories=tuple(BudgetCategory),
    )

    allocation = allocate_budget(compile_planner_context(request))

    assert allocation.status == BudgetAllocationStatus.ALLOCATED
    assert [item.target_amount for item in allocation.allocations] == [
        Decimal("105.00"),
        Decimal("60.00"),
        Decimal("75.00"),
        Decimal("30.00"),
        Decimal("15.00"),
        Decimal("15.00"),
    ]
    assert allocation.allocations[0].quantity_basis == BudgetQuantityBasis.ROOM_NIGHT
    assert allocation.allocations[0].reference_quantity == Decimal("2")
    assert allocation.allocations[0].target_per_unit == Decimal("52.50")
    assert allocation.allocations[1].quantity_basis == BudgetQuantityBasis.PARTY_DAY
    assert allocation.allocations[1].reference_quantity == Decimal("3")
    assert allocation.allocations[3].quantity_basis == BudgetQuantityBasis.TRAVELER_TRIP
    assert allocation.allocations[3].reference_quantity == Decimal("2")


def test_budget_without_request_is_explicitly_not_allocated() -> None:
    context = compile_planner_context(fanout_case("specialist-fanout-complete-v1").request)

    allocation = allocate_budget(context)

    assert allocation.status == BudgetAllocationStatus.NOT_REQUESTED
    assert allocation.reason == BudgetAllocationReason.MISSING_BUDGET
    assert allocation.allocations == ()


def test_lodging_budget_without_rooms_blocks_before_arithmetic() -> None:
    context = compile_planner_context(fanout_case("specialist-fanout-missing-rooms-v1").request)

    allocation = allocate_budget(context)

    assert allocation.status == BudgetAllocationStatus.BLOCKED
    assert allocation.reason == BudgetAllocationReason.MISSING_ROOMS
    assert allocation.allocations == ()


def test_budget_estimate_keeps_lodging_unknown_when_room_count_is_missing() -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout("specialist-fanout-missing-rooms-v1")
        bundle = await build_planning_material_bundle(specialist_result, ScenarioRouteProvider())

        estimate = bundle.budget_estimate
        assert estimate is not None
        assert estimate.status.value == "partial"
        assert estimate.total is None
        assert estimate.unknown_categories == (BudgetCategory.LODGING,)
        assert estimate.comparison_status == BudgetComparisonStatus.INCOMPLETE

    asyncio.run(exercise())


def test_budget_estimate_uses_candidate_stay_range_without_claiming_live_price() -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout(
            budget=BudgetConstraint(
                total_limit=Decimal("3000.00"),
                included_categories=tuple(BudgetCategory),
            )
        )
        bundle = await build_planning_material_bundle(
            specialist_result,
            ScenarioRouteProvider(),
        )
        stay = bundle.shortlist.primary_stay
        assert stay is not None
        priced_stay = stay.model_copy(
            update={
                "nightly_price_estimate": MoneyRange(minimum="400", maximum="600"),
                "price_basis": StayPriceBasis.FIXTURE_ESTIMATE,
                "price_source": stay.source,
            }
        )
        shortlist = bundle.shortlist.model_copy(update={"primary_stay": priced_stay})

        estimate = estimate_trip_budget(
            specialist_result.planner_context,
            shortlist,
        )

        lodging = estimate.items[0]
        assert lodging.category == BudgetCategory.LODGING
        assert lodging.method == BudgetEstimateMethod.CANDIDATE_PRICE_RANGE
        assert lodging.unit_price == MoneyRange(minimum="400", maximum="600")
        assert lodging.total == MoneyRange(minimum="800", maximum="1200")

    asyncio.run(exercise())


def test_budget_estimate_flags_when_even_the_lower_range_exceeds_budget() -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout(
            budget=BudgetConstraint(
                total_limit=Decimal("500.00"),
                included_categories=(
                    BudgetCategory.TRANSPORT,
                    BudgetCategory.FOOD,
                    BudgetCategory.ADMISSION,
                    BudgetCategory.ACTIVITY,
                ),
                hard_limit=False,
            )
        )
        bundle = await build_planning_material_bundle(
            specialist_result,
            ScenarioRouteProvider(),
        )

        estimate = bundle.budget_estimate
        assert estimate is not None
        assert estimate.total == MoneyRange(minimum="720.00", maximum="2880.00")
        assert estimate.comparison_status == BudgetComparisonStatus.OVER_BUDGET
        assert {item.value for item in estimate.advice_codes} == {
            "prioritize_free_activities",
            "use_public_transport",
        }

    asyncio.run(exercise())


def test_one_route_timeout_preserves_other_edges_and_returns_partial_bundle() -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout(
            budget=BudgetConstraint(
                total_limit=Decimal("3000.00"),
                included_categories=tuple(BudgetCategory),
            )
        )
        shortlist = build_planning_shortlist(specialist_result)
        pair = (
            shortlist.poi_candidates[0].candidate_id,
            shortlist.poi_candidates[1].candidate_id,
        )
        provider = ScenarioRouteProvider(failed_pairs={pair})

        bundle = await build_planning_material_bundle(specialist_result, provider)

        assert bundle.status == PlanningMaterialStatus.PARTIAL
        assert bundle.issues == (PlanningMaterialIssueCode.ROUTE_MATRIX_INCOMPLETE,)
        assert bundle.route_matrix.status == RouteMatrixStatus.PARTIAL
        assert bundle.route_matrix.succeeded_edge_count == 11
        assert bundle.route_matrix.failed_edge_count == 1
        failure = next(
            item.failure
            for item in bundle.route_matrix.edges
            if item.status == RouteEdgeStatus.FAILED
        )
        assert failure is not None
        assert failure.category == RouteFailureCategory.PROVIDER
        assert failure.provider_category == ProviderErrorCategory.TIMEOUT
        assert failure.retryable is True
        assert bundle.budget_allocation.status == BudgetAllocationStatus.ALLOCATED

    asyncio.run(exercise())


def test_all_route_failures_are_typed_without_losing_candidate_scope() -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout()
        provider = ScenarioRouteProvider(fail_all=True)

        bundle = await build_planning_material_bundle(specialist_result, provider)

        assert bundle.status == PlanningMaterialStatus.PARTIAL
        assert bundle.route_matrix.status == RouteMatrixStatus.FAILED
        assert bundle.route_matrix.failed_edge_count == 12
        assert len(bundle.route_matrix.poi_candidate_ids) == 3
        assert all(item.failure is not None for item in bundle.route_matrix.edges)

    asyncio.run(exercise())


def test_unsupported_city_blocks_route_provider_calls() -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout("specialist-fanout-unsupported-city-v1")
        provider = ScenarioRouteProvider()

        bundle = await build_planning_material_bundle(specialist_result, provider)

        assert bundle.status == PlanningMaterialStatus.BLOCKED
        assert bundle.route_matrix.status == RouteMatrixStatus.BLOCKED
        assert bundle.route_matrix.reason == RouteMatrixReason.CAPABILITY_BLOCKED
        assert provider.calls == []

    asyncio.run(exercise())


def test_explore_failure_blocks_route_material_without_losing_other_specialists() -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout("specialist-fanout-explore-timeout-v1")
        provider = ScenarioRouteProvider()

        bundle = await build_planning_material_bundle(specialist_result, provider)

        assert bundle.status == PlanningMaterialStatus.BLOCKED
        assert bundle.route_matrix.status == RouteMatrixStatus.UNAVAILABLE
        assert bundle.route_matrix.reason == RouteMatrixReason.NO_EXPLORE_CANDIDATES
        assert bundle.shortlist.primary_stay is not None
        assert provider.calls == []

    asyncio.run(exercise())


def test_route_provider_endpoint_mismatch_is_not_silently_downgraded() -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout()
        shortlist = build_planning_shortlist(specialist_result)

        with pytest.raises(PlanningMaterialProtocolError, match="endpoints"):
            await build_route_matrix(
                specialist_result,
                shortlist,
                ScenarioRouteProvider(return_wrong_endpoint=True),
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("max_concurrency", [0, 9])
def test_route_concurrency_bounds_are_validated(max_concurrency: int) -> None:
    async def exercise() -> None:
        specialist_result = await build_fanout()
        with pytest.raises(ValueError, match="between one and eight"):
            await build_route_matrix(
                specialist_result,
                build_planning_shortlist(specialist_result),
                ScenarioRouteProvider(),
                max_concurrency=max_concurrency,
            )

    asyncio.run(exercise())
