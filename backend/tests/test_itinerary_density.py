import asyncio
from datetime import timedelta

import pytest

from app.domain.planning import ActivityKind
from app.domain.request import ConstraintSet, TripPace, TripRequest
from app.domain.sources import DataMode
from app.evaluation import load_vertical_slice_suite
from app.itinerary_quality import MAX_MEAL_RECOMMENDATION_DISTANCE_METERS
from app.planning.material_contracts import PlanningMaterialStatus
from app.tasks.product_fixture import FixtureProductPlanningPipeline


def _request(*, pace: TripPace, day_count: int) -> TripRequest:
    base = load_vertical_slice_suite().cases[0].request
    return TripRequest.model_validate(
        {
            **base.model_dump(mode="python"),
            "request_id": f"itinerary-density-{pace.value}-{day_count}-days",
            "end_date": base.start_date + timedelta(days=day_count - 1),
            "pace": pace,
            "constraints": ConstraintSet(),
        }
    )


@pytest.mark.parametrize(
    ("pace", "day_count", "expected_activities_per_day"),
    (
        (TripPace.RELAXED, 2, 2),
        (TripPace.STANDARD, 3, 3),
        (TripPace.STANDARD, 5, 3),
    ),
)
def test_fixture_itinerary_density_and_nearby_meals_are_separate(
    pace: TripPace,
    day_count: int,
    expected_activities_per_day: int,
) -> None:
    async def scenario() -> None:
        request = _request(pace=pace, day_count=day_count)
        pipeline = FixtureProductPlanningPipeline(request)
        specialists = await pipeline.run_specialists(request, data_mode=DataMode.FIXTURE)
        materials = await pipeline.build_materials(specialists)

        assert materials.status == PlanningMaterialStatus.READY
        expected_activity_count = day_count * expected_activities_per_day
        assert len(materials.shortlist.poi_candidates) == expected_activity_count
        assert len(materials.route_matrix.edges) == expected_activity_count
        assert all(
            len(cluster.poi_candidate_ids) == expected_activities_per_day
            for cluster in materials.shortlist.day_clusters
        )

        result = pipeline.run_plan(request, materials)
        assert result.plan is not None
        assert len(result.plan.days) == day_count
        for day in result.plan.days:
            activities = tuple(item for item in day.items if item.kind == ActivityKind.ATTRACTION)
            assert len(activities) == expected_activities_per_day
            assert all(item.kind != ActivityKind.MEAL for item in day.items)
            activity_ids = {
                item.candidate_id for item in activities if item.candidate_id is not None
            }
            assert 1 <= len(day.meal_recommendations) <= 2
            assert all(
                recommendation.anchor_candidate_id in activity_ids
                and recommendation.candidate.candidate_id not in activity_ids
                and recommendation.straight_line_distance_meters
                <= MAX_MEAL_RECOMMENDATION_DISTANCE_METERS
                for recommendation in day.meal_recommendations
            )
            first = activities[0]
            assert first.route_from_previous is not None
            assert day.departure_from_stay_at == first.start_at - timedelta(
                minutes=first.route_from_previous.duration_minutes
            )

    asyncio.run(scenario())
