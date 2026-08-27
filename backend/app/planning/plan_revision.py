import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from time import perf_counter

from app.domain.candidates import CandidatePOI, CandidateStay
from app.domain.planning import (
    ActivityKind,
    DayPlan,
    ItineraryItem,
    MealRecommendation,
    TripPlan,
)
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.domain.travel_data import RouteEndpoint, RouteLeg, RouteMode
from app.itinerary_quality import (
    MAX_MEAL_RECOMMENDATION_DISTANCE_METERS,
    is_meal_candidate,
    straight_line_distance_meters,
)
from app.planning.budget_estimator import estimate_trip_budget
from app.planning.material_builder import allocate_budget, planning_material_issues
from app.planning.material_contracts import (
    PlanningActivityReplacement,
    PlanningCandidateKind,
    PlanningDayCluster,
    PlanningMaterialBundle,
    PlanningMaterialStatus,
    PlanningShortlist,
    RouteEdgeFailure,
    RouteEdgeStatus,
    RouteFailureCategory,
    RouteMatrix,
    RouteMatrixEdge,
    RouteMatrixStatus,
)
from app.planning.revision_contracts import (
    PlanRevisionDiff,
    PlanRevisionOperation,
    PlanRevisionRequest,
    PlanRevisionResult,
)
from app.planning.specialist_contracts import SpecialistName
from app.planning.validator import validate_trip_plan
from app.planning.weather_indoor_recovery_contracts import WeatherIndoorRecoveryResult
from app.providers.errors import ProviderRequestError
from app.providers.ports import RouteRequest

RevisionRouteGetter = Callable[[TripRequest, RouteRequest, DataMode], Awaitable[RouteLeg]]


class PlanRevisionProtocolError(RuntimeError):
    """Raised when a structured revision exceeds its confirmed scope."""


def _plan_id(base_plan: TripPlan, request: PlanRevisionRequest, days: tuple[DayPlan, ...]) -> str:
    payload = {
        "base_plan_id": base_plan.plan_id,
        "revision": request.model_dump(mode="json"),
        "days": [day.model_dump(mode="json") for day in days],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"trip-plan-{digest}"


def _shift_item(item: ItineraryItem, *, minutes: int) -> ItineraryItem:
    shifted = item.model_copy(
        update={
            "start_at": item.start_at + timedelta(minutes=minutes),
            "end_at": item.end_at + timedelta(minutes=minutes),
        }
    )
    return ItineraryItem.model_validate(shifted.model_dump(mode="python"))


def _stable_id(prefix: str, *values: object) -> str:
    material = "|".join(str(value) for value in values)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _meal_recommendations(
    context_id: str,
    day: date,
    anchors: tuple[CandidatePOI, ...],
    candidates: tuple[CandidatePOI, ...],
) -> tuple[MealRecommendation, ...]:
    if not anchors:
        return ()
    ranked: list[tuple[int, CandidatePOI, CandidatePOI]] = []
    for candidate in candidates:
        anchor = min(
            anchors,
            key=lambda item: straight_line_distance_meters(
                item.location,
                candidate.location,
            ),
        )
        distance = straight_line_distance_meters(anchor.location, candidate.location)
        if distance <= MAX_MEAL_RECOMMENDATION_DISTANCE_METERS:
            ranked.append((distance, candidate, anchor))
    selected = sorted(ranked, key=lambda item: (item[0], item[1].candidate_id))[:2]
    return tuple(
        MealRecommendation(
            recommendation_id=_stable_id(
                "meal-recommendation",
                context_id,
                day.isoformat(),
                anchor.candidate_id,
                candidate.candidate_id,
            ),
            anchor_candidate_id=anchor.candidate_id,
            candidate=candidate,
            straight_line_distance_meters=distance,
            reason=f"距当日景点“{anchor.name}”约 {distance} 米的 Provider 候选。",
        )
        for distance, candidate, anchor in selected
    )


def _route_endpoint(candidate: CandidatePOI | CandidateStay) -> RouteEndpoint:
    return RouteEndpoint(
        name=candidate.name,
        candidate_id=candidate.candidate_id,
        location=candidate.location,
    )


def _route_edge_id(origin_id: str, destination_id: str) -> str:
    return _stable_id("route-edge", origin_id, destination_id, "transit")


def _route_failure(error: Exception, origin_id: str, destination_id: str) -> RouteEdgeFailure:
    digest = hashlib.sha256(
        (
            f"{origin_id}|{destination_id}|"
            f"{error.__class__.__module__}|{error.__class__.__qualname__}"
        ).encode()
    ).hexdigest()[:12]
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


def _candidate_kind(candidate: CandidatePOI | CandidateStay) -> PlanningCandidateKind:
    return (
        PlanningCandidateKind.STAY
        if isinstance(candidate, CandidateStay)
        else PlanningCandidateKind.POI
    )


def _validate_revision_scope(
    trip_request: TripRequest,
    base_plan: TripPlan,
    revision: PlanRevisionRequest,
) -> tuple[DayPlan, int]:
    if revision.base_plan_id != base_plan.plan_id:
        raise PlanRevisionProtocolError("revision base plan does not match the current plan")
    if base_plan.request_id != trip_request.request_id:
        raise PlanRevisionProtocolError("revision plan does not belong to the current request")
    target_days = tuple(day for day in base_plan.days if day.date == revision.target_date)
    if len(target_days) != 1:
        raise PlanRevisionProtocolError("revision target date is outside the current plan")
    target_day = target_days[0]
    expected_target = tuple(item.item_id for item in target_day.items)
    expected_protected = tuple(
        item.item_id
        for day in base_plan.days
        if day.date != revision.target_date
        for item in day.items
    )
    if revision.target_item_ids != expected_target:
        raise PlanRevisionProtocolError("revision target items do not match the current target day")
    if revision.protected_item_ids != expected_protected:
        raise PlanRevisionProtocolError("revision protected items do not match unaffected days")
    return target_day, base_plan.days.index(target_day) + 1


def _replacement_candidates(
    materials: PlanningMaterialBundle,
    revision: PlanRevisionRequest,
    weather_indoor_recovery: WeatherIndoorRecoveryResult | None,
) -> dict[str, CandidatePOI]:
    explore_branch = next(
        item
        for item in materials.specialist_result.branches
        if item.specialist == SpecialistName.EXPLORE
    )
    observations = (
        explore_branch.explore_result.observations
        if explore_branch.explore_result is not None
        else ()
    )
    observed_by_id = {item.candidate.candidate_id: item.candidate for item in observations}
    if weather_indoor_recovery is not None:
        observed_by_id.update(
            {
                item.candidate.candidate_id: item.candidate
                for item in weather_indoor_recovery.observations
            }
        )
    scheduled_ids = {item.candidate_id for item in materials.shortlist.poi_candidates}
    replacements: dict[str, CandidatePOI] = {}
    for pair in revision.replacement_pairs:
        replacement = observed_by_id.get(pair.replacement_candidate_id)
        if (
            replacement is None
            or is_meal_candidate(replacement)
            or replacement.city != materials.planner_context.destination.normalized_name
            or replacement.candidate_id in scheduled_ids
        ):
            raise PlanRevisionProtocolError(
                "replacement candidate is not an eligible Explore Provider observation"
            )
        replacements[pair.replaced_item_id] = replacement
    return replacements


def _revised_shortlist(
    materials: PlanningMaterialBundle,
    target_day_number: int,
    replacements: dict[str, CandidatePOI],
) -> PlanningShortlist:
    revised_pois = tuple(
        replacements.get(item.candidate_id, item) for item in materials.shortlist.poi_candidates
    )
    revised_clusters = tuple(
        PlanningDayCluster(
            day_number=cluster.day_number,
            poi_candidate_ids=tuple(
                replacements[candidate_id].candidate_id
                if candidate_id in replacements
                else candidate_id
                for candidate_id in cluster.poi_candidate_ids
            ),
        )
        for cluster in materials.shortlist.day_clusters
    )
    explore_branch = next(
        item
        for item in materials.specialist_result.branches
        if item.specialist == SpecialistName.EXPLORE
    )
    recommendations = (
        tuple(
            item.candidate
            for item in sorted(
                explore_branch.explore_result.recommendations,
                key=lambda item: item.proposal.rank,
            )
        )
        if explore_branch.explore_result is not None
        else ()
    )
    selected_ids = {
        *(item.candidate_id for item in revised_pois),
        *(item.candidate_id for item in materials.shortlist.meal_candidates),
    }
    shortlist = materials.shortlist.model_copy(
        update={
            "poi_candidates": revised_pois,
            "day_clusters": revised_clusters,
            "omitted_poi_ids": tuple(
                item.candidate_id
                for item in recommendations
                if item.candidate_id not in selected_ids
            ),
        }
    )
    if target_day_number > len(revised_clusters):
        raise PlanRevisionProtocolError("replacement target day is outside material clusters")
    return PlanningShortlist.model_validate(shortlist.model_dump(mode="python"))


async def _revised_route_matrix(
    trip_request: TripRequest,
    materials: PlanningMaterialBundle,
    shortlist: PlanningShortlist,
    target_day_number: int,
    get_route: RevisionRouteGetter,
) -> tuple[RouteMatrix, int]:
    stay = shortlist.primary_stay
    if stay is None or trip_request.destination_adcode is None:
        raise PlanRevisionProtocolError("activity replacement requires a routed stay anchor")
    candidates = {item.candidate_id: item for item in shortlist.poi_candidates}
    existing_edges = {
        (item.origin_candidate_id, item.destination_candidate_id): item
        for item in materials.route_matrix.edges
    }
    revised_edges: list[RouteMatrixEdge] = []
    provider_call_count = 0
    started = perf_counter()
    for cluster in shortlist.day_clusters:
        origin: CandidatePOI | CandidateStay = stay
        for candidate_id in cluster.poi_candidate_ids:
            destination = candidates[candidate_id]
            pair = (origin.candidate_id, destination.candidate_id)
            if cluster.day_number != target_day_number:
                existing = existing_edges.get(pair)
                if existing is None:
                    raise PlanRevisionProtocolError(
                        "protected day is missing its persisted route lineage"
                    )
                revised_edges.append(existing)
                origin = destination
                continue
            request = RouteRequest(
                origin=_route_endpoint(origin),
                destination=_route_endpoint(destination),
                mode=RouteMode.TRANSIT,
                city_adcode=trip_request.destination_adcode,
            )
            provider_call_count += 1
            try:
                route = await get_route(trip_request, request, materials.data_mode)
                if route.origin != request.origin or route.destination != request.destination:
                    raise PlanRevisionProtocolError(
                        "revision route Provider returned mismatched endpoints"
                    )
                edge = RouteMatrixEdge(
                    edge_id=_route_edge_id(*pair),
                    origin_candidate_id=pair[0],
                    origin_kind=_candidate_kind(origin),
                    destination_candidate_id=pair[1],
                    destination_kind=PlanningCandidateKind.POI,
                    status=RouteEdgeStatus.SUCCEEDED,
                    route=route,
                )
            except PlanRevisionProtocolError:
                raise
            except Exception as error:
                edge = RouteMatrixEdge(
                    edge_id=_route_edge_id(*pair),
                    origin_candidate_id=pair[0],
                    origin_kind=_candidate_kind(origin),
                    destination_candidate_id=pair[1],
                    destination_kind=PlanningCandidateKind.POI,
                    status=RouteEdgeStatus.FAILED,
                    failure=_route_failure(error, *pair),
                )
            revised_edges.append(edge)
            origin = destination
    edges = tuple(revised_edges)
    succeeded = sum(item.status == RouteEdgeStatus.SUCCEEDED for item in edges)
    failed = len(edges) - succeeded
    status = RouteMatrixStatus.COMPLETE
    if failed == len(edges):
        status = RouteMatrixStatus.FAILED
    elif failed:
        status = RouteMatrixStatus.PARTIAL
    matrix = RouteMatrix(
        request_id=materials.request_id,
        context_id=materials.context_id,
        data_mode=materials.data_mode,
        status=status,
        poi_candidate_ids=tuple(item.candidate_id for item in shortlist.poi_candidates),
        primary_stay_id=stay.candidate_id,
        edges=edges,
        expected_edge_count=len(edges),
        succeeded_edge_count=succeeded,
        failed_edge_count=failed,
        provider_call_count=len(edges),
        max_concurrency=materials.route_matrix.max_concurrency,
        latency_ms=max(round((perf_counter() - started) * 1000), 0),
    )
    return matrix, provider_call_count


def _revised_target_day(
    materials: PlanningMaterialBundle,
    target_day: DayPlan,
    shortlist: PlanningShortlist,
    revision: PlanRevisionRequest,
    route_matrix: RouteMatrix,
    replacements: dict[str, CandidatePOI],
) -> tuple[DayPlan, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    candidates = {item.candidate_id: item for item in shortlist.poi_candidates}
    edges = {
        (item.origin_candidate_id, item.destination_candidate_id): item
        for item in route_matrix.edges
    }
    stay = shortlist.primary_stay
    previous_candidate_id = stay.candidate_id if stay is not None else None
    previous_end = None
    revised_items: list[ItineraryItem] = []
    rescheduled_ids: list[str] = []
    added_ids: list[str] = []
    removed_ids: list[str] = []
    for original in target_day.items:
        replacement = replacements.get(original.item_id)
        candidate_id = (
            replacement.candidate_id if replacement is not None else original.candidate_id
        )
        if candidate_id is None or candidate_id not in candidates:
            raise PlanRevisionProtocolError("target day contains an unsupported itinerary item")
        candidate = candidates[candidate_id]
        edge = (
            edges.get((previous_candidate_id, candidate_id))
            if previous_candidate_id is not None
            else None
        )
        route = (
            edge.route
            if edge is not None
            and edge.status == RouteEdgeStatus.SUCCEEDED
            and edge.route is not None
            else None
        )
        earliest_start = original.start_at
        if previous_end is not None:
            earliest_start = max(
                earliest_start,
                previous_end + timedelta(minutes=route.duration_minutes if route else 0),
            )
        duration = (
            candidate.suggested_duration_minutes or 120
            if replacement is not None
            else round((original.end_at - original.start_at).total_seconds() / 60)
        )
        end_at = earliest_start + timedelta(minutes=duration)
        if end_at.date() != target_day.date:
            raise PlanRevisionProtocolError(
                "replacement would move an item outside its target date"
            )
        if replacement is not None:
            revised = ItineraryItem(
                item_id=_stable_id(
                    "plan-item-revision",
                    materials.context_id,
                    revision.revision_id,
                    candidate_id,
                    earliest_start.isoformat(),
                ),
                kind=original.kind,
                title=candidate.name,
                start_at=earliest_start,
                end_at=end_at,
                candidate_id=candidate.candidate_id,
                source=candidate.source,
                route_from_previous=route,
                notes=(
                    "用户从原 Explore Provider observations 中确认了该替换候选。",
                    "仅重新计算受影响日期的路线、时间、用餐建议、预算分配与硬校验。",
                    "实时价格、营业时间和可订状态仍需单独验证。",
                ),
            )
            added_ids.append(revised.item_id)
            removed_ids.append(original.item_id)
        else:
            revised = ItineraryItem.model_validate(
                original.model_copy(
                    update={
                        "start_at": earliest_start,
                        "end_at": end_at,
                        "route_from_previous": route,
                    }
                ).model_dump(mode="python")
            )
            if revised != original:
                rescheduled_ids.append(revised.item_id)
        revised_items.append(revised)
        previous_candidate_id = candidate_id
        previous_end = end_at
    first_route = revised_items[0].route_from_previous
    departure = (
        revised_items[0].start_at - timedelta(minutes=first_route.duration_minutes)
        if first_route is not None and stay is not None
        else None
    )
    anchors = tuple(candidates[item.candidate_id] for item in revised_items if item.candidate_id)
    revised_day = DayPlan(
        date=target_day.date,
        items=tuple(revised_items),
        departure_from_stay_at=departure,
        meal_recommendations=_meal_recommendations(
            materials.context_id,
            target_day.date,
            anchors,
            shortlist.meal_candidates,
        ),
        weather_risk_ids=target_day.weather_risk_ids,
    )
    return (
        revised_day,
        tuple(rescheduled_ids),
        tuple(added_ids),
        tuple(removed_ids),
    )


def apply_plan_revision(
    trip_request: TripRequest,
    base_plan: TripPlan,
    revision: PlanRevisionRequest,
) -> PlanRevisionResult:
    if revision.operation != PlanRevisionOperation.SHIFT_DAY_LATER:
        raise PlanRevisionProtocolError("unsupported plan revision operation")
    _validate_revision_scope(trip_request, base_plan, revision)
    assert revision.shift_minutes is not None

    revised_days: list[DayPlan] = []
    for day in base_plan.days:
        if day.date != revision.target_date:
            revised_days.append(day)
            continue
        shifted_items = tuple(
            _shift_item(item, minutes=revision.shift_minutes) for item in day.items
        )
        if any(
            item.start_at.date() != day.date or item.end_at.date() != day.date
            for item in shifted_items
        ):
            raise PlanRevisionProtocolError("revision would move an item outside its target date")
        revised_days.append(day.model_copy(update={"items": shifted_items}))
        if day.departure_from_stay_at is not None:
            revised_days[-1] = revised_days[-1].model_copy(
                update={
                    "departure_from_stay_at": day.departure_from_stay_at
                    + timedelta(minutes=revision.shift_minutes)
                }
            )

    days = tuple(DayPlan.model_validate(day.model_dump(mode="python")) for day in revised_days)
    revised_plan = TripPlan.model_validate(
        base_plan.model_copy(
            update={
                "plan_id": _plan_id(base_plan, revision, days),
                "days": days,
            }
        ).model_dump(mode="python")
    )

    for before, after in zip(base_plan.days, revised_plan.days, strict=True):
        if before.date != revision.target_date and before != after:
            raise PlanRevisionProtocolError("revision changed a protected day")
    if (
        revised_plan.cost_items != base_plan.cost_items
        or revised_plan.weather_risks != base_plan.weather_risks
        or revised_plan.destination_city != base_plan.destination_city
        or revised_plan.start_date != base_plan.start_date
        or revised_plan.end_date != base_plan.end_date
    ):
        raise PlanRevisionProtocolError("revision changed protected plan facts")

    validation = validate_trip_plan(trip_request, revised_plan)
    return PlanRevisionResult(
        request=revision,
        revised_plan=revised_plan,
        validation=validation,
        diff=PlanRevisionDiff(
            from_plan_id=base_plan.plan_id,
            to_plan_id=revised_plan.plan_id,
            changed_dates=(revision.target_date,),
            rescheduled_item_ids=revision.target_item_ids,
        ),
    )


async def apply_activity_replacement(
    trip_request: TripRequest,
    base_plan: TripPlan,
    materials: PlanningMaterialBundle,
    revision: PlanRevisionRequest,
    get_route: RevisionRouteGetter,
    *,
    weather_indoor_recovery: WeatherIndoorRecoveryResult | None = None,
) -> PlanRevisionResult:
    if revision.operation != PlanRevisionOperation.REPLACE_ACTIVITY:
        raise PlanRevisionProtocolError("activity replacement requires replace_activity")
    target_day, target_day_number = _validate_revision_scope(
        trip_request,
        base_plan,
        revision,
    )
    target_items = {item.item_id: item for item in target_day.items}
    replacement_items = []
    for pair in revision.replacement_pairs:
        target_item = target_items.get(pair.replaced_item_id)
        if (
            target_item is None
            or target_item.kind != ActivityKind.ATTRACTION
            or target_item.candidate_id is None
        ):
            raise PlanRevisionProtocolError("replacement target is not a grounded itinerary item")
        replacement_items.append(target_item)
    replacements_by_item_id = _replacement_candidates(
        materials,
        revision,
        weather_indoor_recovery,
    )
    replacements_by_candidate_id = {
        target_item.candidate_id: replacements_by_item_id[target_item.item_id]
        for target_item in replacement_items
        if target_item.candidate_id is not None
    }
    shortlist = _revised_shortlist(
        materials,
        target_day_number,
        replacements_by_candidate_id,
    )
    route_matrix, provider_call_count = await _revised_route_matrix(
        trip_request,
        materials,
        shortlist,
        target_day_number,
        get_route,
    )
    budget_allocation = allocate_budget(materials.planner_context)
    budget_estimate = estimate_trip_budget(
        materials.planner_context,
        shortlist,
        route_matrix,
    )
    issues = planning_material_issues(
        materials.specialist_result,
        shortlist,
        route_matrix,
        budget_allocation,
    )
    replacement_records = tuple(
        PlanningActivityReplacement(
            revision_id=revision.revision_id,
            target_day_number=target_day_number,
            removed_candidate_id=target_item.candidate_id,
            replacement_candidate_id=replacements_by_item_id[target_item.item_id].candidate_id,
        )
        for target_item in replacement_items
        if target_item.candidate_id is not None
    )
    prior_replacement_records = materials.activity_replacements or (
        (materials.activity_replacement,) if materials.activity_replacement is not None else ()
    )
    all_replacement_records = (*prior_replacement_records, *replacement_records)
    revised_materials = PlanningMaterialBundle(
        request_id=materials.request_id,
        context_id=materials.context_id,
        data_mode=materials.data_mode,
        status=PlanningMaterialStatus.PARTIAL if issues else PlanningMaterialStatus.READY,
        issues=issues,
        planner_context=materials.planner_context,
        specialist_result=materials.specialist_result,
        shortlist=shortlist,
        route_matrix=route_matrix,
        budget_allocation=budget_allocation,
        budget_estimate=budget_estimate,
        activity_replacement=(
            all_replacement_records[0] if len(all_replacement_records) == 1 else None
        ),
        activity_replacements=(
            all_replacement_records if len(all_replacement_records) > 1 else ()
        ),
        weather_indoor_recovery=weather_indoor_recovery,
    )
    revised_day, rescheduled_ids, added_ids, removed_ids = _revised_target_day(
        revised_materials,
        target_day,
        shortlist,
        revision,
        route_matrix,
        replacements_by_item_id,
    )
    days = tuple(revised_day if day.date == revision.target_date else day for day in base_plan.days)
    revised_plan = TripPlan.model_validate(
        base_plan.model_copy(
            update={
                "plan_id": _plan_id(base_plan, revision, days),
                "days": days,
            }
        ).model_dump(mode="python")
    )
    for before, after in zip(base_plan.days, revised_plan.days, strict=True):
        if before.date != revision.target_date and before != after:
            raise PlanRevisionProtocolError("replacement changed a protected day")
    if (
        revised_plan.cost_items != base_plan.cost_items
        or revised_plan.weather_risks != base_plan.weather_risks
        or revised_plan.destination_city != base_plan.destination_city
        or revised_plan.start_date != base_plan.start_date
        or revised_plan.end_date != base_plan.end_date
    ):
        raise PlanRevisionProtocolError("replacement changed protected plan facts")
    return PlanRevisionResult(
        executor_version=(
            "deterministic-local-revision-v3"
            if len(revision.replacement_pairs) > 1
            else "deterministic-local-revision-v2"
        ),
        request=revision,
        revised_plan=revised_plan,
        validation=validate_trip_plan(trip_request, revised_plan),
        diff=PlanRevisionDiff(
            from_plan_id=base_plan.plan_id,
            to_plan_id=revised_plan.plan_id,
            changed_dates=(revision.target_date,),
            rescheduled_item_ids=rescheduled_ids,
            added_item_ids=added_ids,
            removed_item_ids=removed_ids,
        ),
        revised_materials=revised_materials,
        reused_provider_results=False,
        provider_call_count=provider_call_count,
    )
