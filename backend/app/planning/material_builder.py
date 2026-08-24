import asyncio
import hashlib
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from time import perf_counter

from app.domain.candidates import CandidatePOI, CandidateStay
from app.domain.context import PlannerCapability, PlannerContext
from app.domain.money import BudgetCategory
from app.domain.travel_data import RouteEndpoint, RouteMode
from app.itinerary_quality import (
    EXCESSIVE_TRANSFER_MINUTES,
    cluster_major_activities,
    is_meal_candidate,
    major_activity_range,
    major_activity_target,
    select_major_activities,
)
from app.planning.material_contracts import (
    BudgetAllocation,
    BudgetAllocationItem,
    BudgetAllocationReason,
    BudgetAllocationStatus,
    BudgetQuantityBasis,
    PlanningCandidateKind,
    PlanningDayCluster,
    PlanningMaterialBundle,
    PlanningMaterialIssueCode,
    PlanningMaterialStatus,
    PlanningShortlist,
    RouteEdgeFailure,
    RouteEdgeStatus,
    RouteFailureCategory,
    RouteMatrix,
    RouteMatrixEdge,
    RouteMatrixReason,
    RouteMatrixStatus,
)
from app.planning.specialist_contracts import (
    SpecialistFanoutResult,
    SpecialistFanoutStatus,
    SpecialistName,
)
from app.providers.errors import ProviderRequestError
from app.providers.ports import RouteProvider, RouteRequest

DEFAULT_ROUTE_CONCURRENCY = 4
MONEY_QUANTUM = Decimal("0.01")
DEFAULT_BUDGET_WEIGHTS: dict[BudgetCategory, Decimal] = {
    BudgetCategory.LODGING: Decimal("0.3500"),
    BudgetCategory.TRANSPORT: Decimal("0.2000"),
    BudgetCategory.FOOD: Decimal("0.2500"),
    BudgetCategory.ADMISSION: Decimal("0.1000"),
    BudgetCategory.ACTIVITY: Decimal("0.0500"),
    BudgetCategory.OTHER: Decimal("0.0500"),
}


class PlanningMaterialProtocolError(RuntimeError):
    """Raised when a route Provider violates the typed planning-material contract."""


def build_planning_shortlist(result: SpecialistFanoutResult) -> PlanningShortlist:
    explore_branch = next(
        item for item in result.branches if item.specialist == SpecialistName.EXPLORE
    )
    explore_recommendations = (
        tuple(
            sorted(
                explore_branch.explore_result.recommendations,
                key=lambda item: item.proposal.rank,
            )
        )
        if explore_branch.explore_result is not None
        else ()
    )
    stay_branch = next(item for item in result.branches if item.specialist == SpecialistName.STAY)
    stay_recommendations = (
        tuple(
            sorted(
                stay_branch.stay_result.recommendations,
                key=lambda item: item.proposal.rank,
            )
        )
        if stay_branch.stay_result is not None
        else ()
    )
    available_pois = tuple(item.candidate for item in explore_recommendations)
    available_activity_count = sum(not is_meal_candidate(item) for item in available_pois)
    target = major_activity_target(
        result.planner_context.day_count,
        result.planner_context.pace,
        available_count=available_activity_count,
    )
    minimum_per_day, _ = major_activity_range(result.planner_context.pace)
    primary_stay = stay_recommendations[0].candidate if stay_recommendations else None
    activities = select_major_activities(available_pois, primary_stay, target=target)
    meals = tuple(item for item in available_pois if is_meal_candidate(item))[
        : result.planner_context.day_count * 3
    ]
    groups = cluster_major_activities(
        activities,
        primary_stay,
        day_count=result.planner_context.day_count,
    )
    selected_ids = {
        *(item.candidate_id for item in activities),
        *(item.candidate_id for item in meals),
    }
    return PlanningShortlist(
        activity_target_per_day=minimum_per_day,
        poi_candidates=activities,
        meal_candidates=meals,
        day_clusters=tuple(
            PlanningDayCluster(
                day_number=index,
                poi_candidate_ids=tuple(item.candidate_id for item in group),
            )
            for index, group in enumerate(groups, start=1)
        ),
        primary_stay=primary_stay,
        omitted_poi_ids=tuple(
            item.candidate_id for item in available_pois if item.candidate_id not in selected_ids
        ),
        omitted_stay_ids=tuple(item.candidate.candidate_id for item in stay_recommendations[1:]),
    )


def _quantity_basis(
    category: BudgetCategory,
    context: PlannerContext,
) -> tuple[BudgetQuantityBasis, Decimal]:
    if category == BudgetCategory.LODGING:
        if context.party.room_nights is None:
            raise PlanningMaterialProtocolError(
                "lodging allocation requires deterministic room_nights"
            )
        return BudgetQuantityBasis.ROOM_NIGHT, Decimal(context.party.room_nights)
    if category in {BudgetCategory.TRANSPORT, BudgetCategory.FOOD}:
        return BudgetQuantityBasis.PARTY_DAY, Decimal(context.day_count)
    if category in {BudgetCategory.ADMISSION, BudgetCategory.ACTIVITY}:
        return BudgetQuantityBasis.TRAVELER_TRIP, Decimal(context.party.total_travelers)
    return BudgetQuantityBasis.PARTY_TRIP, Decimal("1")


def _allocate_targets(
    total_limit: Decimal,
    categories: tuple[BudgetCategory, ...],
) -> dict[BudgetCategory, Decimal]:
    included_weight = sum(
        (DEFAULT_BUDGET_WEIGHTS[category] for category in categories),
        start=Decimal("0"),
    )
    raw_targets = {
        category: total_limit * DEFAULT_BUDGET_WEIGHTS[category] / included_weight
        for category in categories
    }
    targets = {
        category: amount.quantize(MONEY_QUANTUM, rounding=ROUND_DOWN)
        for category, amount in raw_targets.items()
    }
    remaining_cents = int((total_limit - sum(targets.values(), start=Decimal("0"))) / MONEY_QUANTUM)
    order = {category: index for index, category in enumerate(BudgetCategory)}
    remainder_order = sorted(
        categories,
        key=lambda category: (
            -(raw_targets[category] - targets[category]),
            order[category],
        ),
    )
    for category in remainder_order[:remaining_cents]:
        targets[category] += MONEY_QUANTUM
    return targets


def allocate_budget(context: PlannerContext) -> BudgetAllocation:
    if context.budget is None:
        return BudgetAllocation(
            request_id=context.request_id,
            context_id=context.context_id,
            input_request_sha256=context.input_request_sha256,
            status=BudgetAllocationStatus.NOT_REQUESTED,
            reason=BudgetAllocationReason.MISSING_BUDGET,
        )
    if PlannerCapability.BUDGET_VALIDATION not in context.ready_capabilities:
        return BudgetAllocation(
            request_id=context.request_id,
            context_id=context.context_id,
            input_request_sha256=context.input_request_sha256,
            status=BudgetAllocationStatus.BLOCKED,
            reason=BudgetAllocationReason.MISSING_ROOMS,
        )
    categories = tuple(
        category
        for category in BudgetCategory
        if category in set(context.budget.included_categories)
    )
    targets = _allocate_targets(context.budget.total_limit, categories)
    allocations: list[BudgetAllocationItem] = []
    for category in categories:
        basis, quantity = _quantity_basis(category, context)
        target = targets[category]
        allocations.append(
            BudgetAllocationItem(
                category=category,
                policy_weight=DEFAULT_BUDGET_WEIGHTS[category],
                target_amount=target,
                quantity_basis=basis,
                reference_quantity=quantity,
                target_per_unit=(target / quantity).quantize(
                    MONEY_QUANTUM,
                    rounding=ROUND_HALF_UP,
                ),
            )
        )
    return BudgetAllocation(
        request_id=context.request_id,
        context_id=context.context_id,
        input_request_sha256=context.input_request_sha256,
        status=BudgetAllocationStatus.ALLOCATED,
        total_limit=context.budget.total_limit,
        hard_limit=context.budget.hard_limit,
        included_categories=categories,
        excluded_categories=tuple(
            category for category in BudgetCategory if category not in set(categories)
        ),
        allocations=tuple(allocations),
    )


def _route_endpoint(candidate: CandidatePOI | CandidateStay) -> RouteEndpoint:
    return RouteEndpoint(
        name=candidate.name,
        candidate_id=candidate.candidate_id,
        location=candidate.location,
    )


def _candidate_kind(candidate: CandidatePOI | CandidateStay) -> PlanningCandidateKind:
    if isinstance(candidate, CandidateStay):
        return PlanningCandidateKind.STAY
    return PlanningCandidateKind.POI


def _route_edge_id(origin_id: str, destination_id: str) -> str:
    digest = hashlib.sha256(f"{origin_id}|{destination_id}|transit".encode()).hexdigest()[:16]
    return f"route-edge-{digest}"


def _route_failure(error: Exception, origin_id: str, destination_id: str) -> RouteEdgeFailure:
    material = (
        f"{origin_id}|{destination_id}|{error.__class__.__module__}|{error.__class__.__qualname__}"
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    if isinstance(error, ProviderRequestError):
        return RouteEdgeFailure(
            category=RouteFailureCategory.PROVIDER,
            error_code=f"route-provider-{digest}",
            retryable=error.failure.retryable,
            provider_category=error.failure.category,
        )
    return RouteEdgeFailure(
        category=RouteFailureCategory.DEPENDENCY,
        error_code=f"route-dependency-{digest}",
        retryable=False,
    )


def _candidate_pairs(
    shortlist: PlanningShortlist,
    *,
    use_legacy_matrix: bool,
) -> tuple[tuple[CandidatePOI | CandidateStay, CandidatePOI | CandidateStay], ...]:
    if use_legacy_matrix:
        pairs: list[tuple[CandidatePOI | CandidateStay, CandidatePOI | CandidateStay]] = [
            (origin, destination)
            for origin in shortlist.poi_candidates
            for destination in shortlist.poi_candidates
            if origin.candidate_id != destination.candidate_id
        ]
        if shortlist.primary_stay is not None:
            for poi in shortlist.poi_candidates:
                pairs.extend(((shortlist.primary_stay, poi), (poi, shortlist.primary_stay)))
        return tuple(pairs)
    if shortlist.primary_stay is None:
        return ()
    candidates_by_id = {item.candidate_id: item for item in shortlist.poi_candidates}
    pairs = []
    for cluster in shortlist.day_clusters:
        origin: CandidatePOI | CandidateStay = shortlist.primary_stay
        for candidate_id in cluster.poi_candidate_ids:
            destination = candidates_by_id[candidate_id]
            pairs.append((origin, destination))
            origin = destination
    return tuple(pairs)


async def build_route_matrix(
    result: SpecialistFanoutResult,
    shortlist: PlanningShortlist,
    provider: RouteProvider,
    *,
    max_concurrency: int = DEFAULT_ROUTE_CONCURRENCY,
) -> RouteMatrix:
    if not 1 <= max_concurrency <= 8:
        raise ValueError("route matrix max_concurrency must be between one and eight")
    context = result.planner_context
    poi_ids = tuple(item.candidate_id for item in shortlist.poi_candidates)
    stay_id = shortlist.primary_stay.candidate_id if shortlist.primary_stay is not None else None
    if PlannerCapability.ROUTE_PLANNING not in context.ready_capabilities:
        return RouteMatrix(
            request_id=result.request_id,
            context_id=result.context_id,
            data_mode=result.data_mode,
            status=RouteMatrixStatus.BLOCKED,
            reason=RouteMatrixReason.CAPABILITY_BLOCKED,
            poi_candidate_ids=poi_ids,
            primary_stay_id=stay_id,
            edges=(),
            expected_edge_count=0,
            succeeded_edge_count=0,
            failed_edge_count=0,
            provider_call_count=0,
            max_concurrency=max_concurrency,
            latency_ms=0,
        )
    if context.destination.administrative_code is None:
        raise PlanningMaterialProtocolError(
            "route capability is ready without a destination administrative code"
        )
    pairs = _candidate_pairs(shortlist, use_legacy_matrix=context.pace is None)
    if not poi_ids:
        return RouteMatrix(
            request_id=result.request_id,
            context_id=result.context_id,
            data_mode=result.data_mode,
            status=RouteMatrixStatus.UNAVAILABLE,
            reason=RouteMatrixReason.NO_EXPLORE_CANDIDATES,
            poi_candidate_ids=(),
            primary_stay_id=stay_id,
            edges=(),
            expected_edge_count=0,
            succeeded_edge_count=0,
            failed_edge_count=0,
            provider_call_count=0,
            max_concurrency=max_concurrency,
            latency_ms=0,
        )
    if not pairs:
        return RouteMatrix(
            request_id=result.request_id,
            context_id=result.context_id,
            data_mode=result.data_mode,
            status=RouteMatrixStatus.NOT_REQUIRED,
            reason=RouteMatrixReason.INSUFFICIENT_CANDIDATE_PAIR,
            poi_candidate_ids=poi_ids,
            primary_stay_id=stay_id,
            edges=(),
            expected_edge_count=0,
            succeeded_edge_count=0,
            failed_edge_count=0,
            provider_call_count=0,
            max_concurrency=max_concurrency,
            latency_ms=0,
        )
    semaphore = asyncio.Semaphore(max_concurrency)

    async def build_edge(
        origin: CandidatePOI | CandidateStay,
        destination: CandidatePOI | CandidateStay,
    ) -> RouteMatrixEdge:
        origin_endpoint = _route_endpoint(origin)
        destination_endpoint = _route_endpoint(destination)
        try:
            async with semaphore:
                route = await provider.get_route(
                    RouteRequest(
                        origin=origin_endpoint,
                        destination=destination_endpoint,
                        mode=RouteMode.TRANSIT,
                        city_adcode=context.destination.administrative_code,
                    )
                )
        except Exception as error:
            return RouteMatrixEdge(
                edge_id=_route_edge_id(origin.candidate_id, destination.candidate_id),
                origin_candidate_id=origin.candidate_id,
                origin_kind=_candidate_kind(origin),
                destination_candidate_id=destination.candidate_id,
                destination_kind=_candidate_kind(destination),
                status=RouteEdgeStatus.FAILED,
                failure=_route_failure(error, origin.candidate_id, destination.candidate_id),
            )
        if route.origin != origin_endpoint or route.destination != destination_endpoint:
            raise PlanningMaterialProtocolError(
                "route Provider returned endpoints that differ from the requested candidates"
            )
        return RouteMatrixEdge(
            edge_id=_route_edge_id(origin.candidate_id, destination.candidate_id),
            origin_candidate_id=origin.candidate_id,
            origin_kind=_candidate_kind(origin),
            destination_candidate_id=destination.candidate_id,
            destination_kind=_candidate_kind(destination),
            status=RouteEdgeStatus.SUCCEEDED,
            route=route,
        )

    started = perf_counter()
    edges = tuple(await asyncio.gather(*(build_edge(*pair) for pair in pairs)))
    latency_ms = max(round((perf_counter() - started) * 1000), 0)
    succeeded = sum(item.status == RouteEdgeStatus.SUCCEEDED for item in edges)
    failed = len(edges) - succeeded
    status = RouteMatrixStatus.COMPLETE
    if failed == len(edges):
        status = RouteMatrixStatus.FAILED
    elif failed:
        status = RouteMatrixStatus.PARTIAL
    return RouteMatrix(
        request_id=result.request_id,
        context_id=result.context_id,
        data_mode=result.data_mode,
        status=status,
        poi_candidate_ids=poi_ids,
        primary_stay_id=stay_id,
        edges=edges,
        expected_edge_count=len(edges),
        succeeded_edge_count=succeeded,
        failed_edge_count=failed,
        provider_call_count=len(edges),
        max_concurrency=max_concurrency,
        latency_ms=latency_ms,
    )


def _planning_material_issues(
    specialist_result: SpecialistFanoutResult,
    shortlist: PlanningShortlist,
    route_matrix: RouteMatrix,
    budget_allocation: BudgetAllocation,
) -> tuple[PlanningMaterialIssueCode, ...]:
    issues: list[PlanningMaterialIssueCode] = []
    if specialist_result.status != SpecialistFanoutStatus.COMPLETE:
        issues.append(PlanningMaterialIssueCode.SPECIALIST_INCOMPLETE)
    if route_matrix.status not in {
        RouteMatrixStatus.COMPLETE,
        RouteMatrixStatus.NOT_REQUIRED,
    }:
        issues.append(PlanningMaterialIssueCode.ROUTE_MATRIX_INCOMPLETE)
    if budget_allocation.status != BudgetAllocationStatus.ALLOCATED:
        issues.append(PlanningMaterialIssueCode.BUDGET_NOT_ALLOCATED)
    if shortlist.primary_stay is None:
        issues.append(PlanningMaterialIssueCode.STAY_ANCHOR_MISSING)
    if specialist_result.planner_context.pace is not None and len(
        shortlist.poi_candidates
    ) < major_activity_target(
        specialist_result.planner_context.day_count,
        specialist_result.planner_context.pace,
    ):
        issues.append(PlanningMaterialIssueCode.ACTIVITY_COVERAGE_INSUFFICIENT)
    if specialist_result.planner_context.pace is not None and any(
        edge.route is not None and edge.route.duration_minutes > EXCESSIVE_TRANSFER_MINUTES
        for edge in route_matrix.edges
    ):
        issues.append(PlanningMaterialIssueCode.EXCESSIVE_TRANSFER)
    return tuple(issues)


async def build_planning_material_bundle(
    specialist_result: SpecialistFanoutResult,
    route_provider: RouteProvider,
    *,
    max_route_concurrency: int = DEFAULT_ROUTE_CONCURRENCY,
) -> PlanningMaterialBundle:
    shortlist = build_planning_shortlist(specialist_result)
    route_matrix = await build_route_matrix(
        specialist_result,
        shortlist,
        route_provider,
        max_concurrency=max_route_concurrency,
    )
    budget_allocation = allocate_budget(specialist_result.planner_context)
    issues = _planning_material_issues(
        specialist_result,
        shortlist,
        route_matrix,
        budget_allocation,
    )
    explore_branch = next(
        item for item in specialist_result.branches if item.specialist == SpecialistName.EXPLORE
    )
    status = PlanningMaterialStatus.READY
    if (
        specialist_result.status == SpecialistFanoutStatus.BLOCKED
        or route_matrix.status in {RouteMatrixStatus.BLOCKED, RouteMatrixStatus.UNAVAILABLE}
        or explore_branch.explore_result is None
    ):
        status = PlanningMaterialStatus.BLOCKED
    elif issues:
        status = PlanningMaterialStatus.PARTIAL
    return PlanningMaterialBundle(
        request_id=specialist_result.request_id,
        context_id=specialist_result.context_id,
        data_mode=specialist_result.data_mode,
        status=status,
        issues=issues,
        planner_context=specialist_result.planner_context,
        specialist_result=specialist_result,
        shortlist=shortlist,
        route_matrix=route_matrix,
        budget_allocation=budget_allocation,
    )
