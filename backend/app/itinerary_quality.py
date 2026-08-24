import math

from app.domain.candidates import CandidatePOI, CandidateStay, GeoPoint
from app.domain.request import TripPace

MAX_MAJOR_ACTIVITIES = 15
LEGACY_MAX_MAJOR_ACTIVITIES = 4
MAX_MEAL_RECOMMENDATION_DISTANCE_METERS = 3000
MAX_ACTIVITY_RADIUS_FROM_STAY_METERS = 30_000
LONG_TRANSFER_WARNING_MINUTES = 60
EXCESSIVE_TRANSFER_MINUTES = 90

_DINING_MARKERS = (
    "餐饮",
    "餐厅",
    "饭店",
    "酒楼",
    "小吃",
    "美食",
    "火锅",
    "烧烤",
    "咖啡",
    "茶饮",
    "restaurant",
    "food",
    "cafe",
    "café",
)


def major_activity_range(pace: TripPace | None) -> tuple[int, int]:
    if pace is None:
        return 1, LEGACY_MAX_MAJOR_ACTIVITIES
    if pace == TripPace.RELAXED:
        return 2, 3
    return 3, 4


def major_activity_target(
    day_count: int,
    pace: TripPace | None,
    *,
    available_count: int | None = None,
) -> int:
    if pace is None:
        return min(
            available_count if available_count is not None else day_count,
            LEGACY_MAX_MAJOR_ACTIVITIES,
        )
    minimum_per_day, _ = major_activity_range(pace)
    return min(day_count * minimum_per_day, MAX_MAJOR_ACTIVITIES)


def is_meal_candidate(candidate: CandidatePOI) -> bool:
    searchable = " ".join((candidate.name, *candidate.categories, *candidate.tags)).casefold()
    return any(marker in searchable for marker in _DINING_MARKERS)


def straight_line_distance_meters(left: GeoPoint, right: GeoPoint) -> int:
    radius_meters = 6_371_000
    left_latitude = math.radians(left.latitude)
    right_latitude = math.radians(right.latitude)
    latitude_delta = right_latitude - left_latitude
    longitude_delta = math.radians(right.longitude - left.longitude)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(left_latitude) * math.cos(right_latitude) * math.sin(longitude_delta / 2) ** 2
    )
    return round(2 * radius_meters * math.asin(math.sqrt(haversine)))


def select_major_activities(
    candidates: tuple[CandidatePOI, ...],
    stay: CandidateStay | None,
    *,
    target: int,
) -> tuple[CandidatePOI, ...]:
    non_meal_candidates = tuple(item for item in candidates if not is_meal_candidate(item))
    if stay is None:
        return non_meal_candidates[:target]
    nearby = tuple(
        item
        for item in non_meal_candidates
        if straight_line_distance_meters(stay.location, item.location)
        <= MAX_ACTIVITY_RADIUS_FROM_STAY_METERS
    )
    return nearby[:target]


def _nearest_neighbor_order(
    candidates: tuple[CandidatePOI, ...],
    stay: CandidateStay | None,
) -> tuple[CandidatePOI, ...]:
    remaining = list(candidates)
    ordered: list[CandidatePOI] = []
    current = stay.location if stay is not None else remaining[0].location
    while remaining:
        next_candidate = min(
            remaining,
            key=lambda candidate: (
                straight_line_distance_meters(current, candidate.location),
                candidate.candidate_id,
            ),
        )
        ordered.append(next_candidate)
        remaining.remove(next_candidate)
        current = next_candidate.location
    return tuple(ordered)


def cluster_major_activities(
    candidates: tuple[CandidatePOI, ...],
    stay: CandidateStay | None,
    *,
    day_count: int,
) -> tuple[tuple[CandidatePOI, ...], ...]:
    """Build balanced geographic day groups before requesting paid Provider routes."""

    if not candidates:
        return tuple(() for _ in range(day_count))
    if len(candidates) < day_count:
        partial_groups = tuple((candidate,) for candidate in candidates)
        return (*partial_groups, *(tuple() for _ in range(day_count - len(partial_groups))))

    base_size, extra = divmod(len(candidates), day_count)
    capacities = [base_size + (1 if index < extra else 0) for index in range(day_count)]
    seeds = [candidates[0]]
    remaining = list(candidates[1:])
    while len(seeds) < day_count:
        seed = max(
            remaining,
            key=lambda candidate: (
                min(
                    straight_line_distance_meters(candidate.location, existing.location)
                    for existing in seeds
                ),
                -candidates.index(candidate),
            ),
        )
        seeds.append(seed)
        remaining.remove(seed)

    groups: list[list[CandidatePOI]] = [[seed] for seed in seeds]
    for candidate in remaining:
        available = [index for index in range(day_count) if len(groups[index]) < capacities[index]]
        group_index = min(
            available,
            key=lambda index: (
                straight_line_distance_meters(candidate.location, seeds[index].location),
                len(groups[index]),
                index,
            ),
        )
        groups[group_index].append(candidate)

    return tuple(_nearest_neighbor_order(tuple(group), stay) for group in groups)
