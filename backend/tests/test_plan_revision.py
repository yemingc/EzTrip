import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.domain.candidates import ActivityEnvironment
from app.domain.planning import ActivityKind, DayPlan, ItineraryItem, TripPlan
from app.domain.request import ConstraintSet, TripPace, TripRequest
from app.domain.sources import DataMode
from app.evaluation import load_vertical_slice_suite, run_vertical_slice_case
from app.itinerary_quality import is_meal_candidate
from app.planning import (
    PlanRevisionOperation,
    PlanRevisionProtocolError,
    PlanRevisionRequest,
    apply_plan_revision,
)
from app.planning.plan_revision import apply_activity_replacement
from app.planning.specialist_contracts import SpecialistName
from app.tasks.product_fixture import FixtureProductPlanningPipeline


def _normal_case_result():
    case = next(
        item for item in load_vertical_slice_suite().cases if item.expected.outcome == "ready"
    )
    result, _ = asyncio.run(run_vertical_slice_case(case))
    return case, result


def _revision(result, *, shift_minutes: int = 120) -> PlanRevisionRequest:
    target_day = result.plan.days[1]
    return PlanRevisionRequest(
        revision_id="revision-day-two-later-v1",
        base_version_id="plan-version-fixture-v1",
        base_plan_id=result.plan.plan_id,
        target_date=target_day.date,
        operation=PlanRevisionOperation.SHIFT_DAY_LATER,
        shift_minutes=shift_minutes,
        target_item_ids=tuple(item.item_id for item in target_day.items),
        protected_item_ids=tuple(
            item.item_id
            for day in result.plan.days
            if day.date != target_day.date
            for item in day.items
        ),
        confirmed=True,
    )


def test_structured_revision_changes_only_target_day_and_revalidates() -> None:
    case, result = _normal_case_result()
    revision = _revision(result)

    revised = apply_plan_revision(case.request, result.plan, revision)

    assert revised.revised_plan.plan_id != result.plan.plan_id
    assert revised.diff.changed_dates == (result.plan.days[1].date,)
    assert revised.diff.rescheduled_item_ids == revision.target_item_ids
    assert revised.revised_plan.days[0] == result.plan.days[0]
    assert revised.revised_plan.days[2] == result.plan.days[2]
    assert revised.revised_plan.days[1].items[0].start_at == (
        result.plan.days[1].items[0].start_at + timedelta(hours=2)
    )
    assert revised.validation.plan_id == revised.revised_plan.plan_id
    assert revised.validation.can_finalize is True
    assert revised.model_call_count == 0
    assert revised.provider_call_count == 0


def test_revision_rejects_stale_base_and_scope_drift() -> None:
    case, result = _normal_case_result()
    revision = _revision(result)

    with pytest.raises(PlanRevisionProtocolError, match="base plan"):
        apply_plan_revision(
            case.request,
            result.plan,
            revision.model_copy(update={"base_plan_id": "trip-plan-stale"}),
        )
    with pytest.raises(PlanRevisionProtocolError, match="protected items"):
        apply_plan_revision(
            case.request,
            result.plan,
            revision.model_copy(update={"protected_item_ids": ()}),
        )


def test_revision_contract_rejects_unconfirmed_or_overlapping_scope() -> None:
    _, result = _normal_case_result()
    revision = _revision(result)
    missing_confirmation = revision.model_dump(mode="json")
    del missing_confirmation["confirmed"]
    with pytest.raises(ValidationError):
        PlanRevisionRequest.model_validate(missing_confirmation)
    with pytest.raises(ValidationError):
        PlanRevisionRequest.model_validate(
            {
                **revision.model_dump(mode="json"),
                "confirmed": False,
            }
        )
    with pytest.raises(ValidationError, match="cannot overlap"):
        PlanRevisionRequest.model_validate(
            {
                **revision.model_dump(mode="json"),
                "protected_item_ids": [revision.target_item_ids[0]],
            }
        )


def test_revision_rejects_a_shift_that_crosses_the_target_date() -> None:
    case, result = _normal_case_result()
    target_day = result.plan.days[1]
    late_item = ItineraryItem.model_validate(
        target_day.items[0]
        .model_copy(
            update={
                "start_at": target_day.items[0].start_at.replace(hour=22),
                "end_at": target_day.items[0].end_at.replace(hour=23),
            }
        )
        .model_dump(mode="python")
    )
    late_day = DayPlan.model_validate(
        target_day.model_copy(update={"items": (late_item,)}).model_dump(mode="python")
    )
    late_plan = TripPlan.model_validate(
        result.plan.model_copy(
            update={
                "plan_id": "trip-plan-late-fixture",
                "days": (result.plan.days[0], late_day, result.plan.days[2]),
            }
        ).model_dump(mode="python")
    )
    late_result = result.model_copy(update={"plan": late_plan})

    with pytest.raises(PlanRevisionProtocolError, match="outside its target date"):
        apply_plan_revision(case.request, late_plan, _revision(late_result, shift_minutes=120))


def test_activity_replacement_uses_observed_candidate_and_recalculates_only_target_day() -> None:
    async def scenario() -> None:
        base = load_vertical_slice_suite().cases[0].request
        request = TripRequest.model_validate(
            {
                **base.model_dump(mode="python"),
                "request_id": "revision-replace-activity-fixture",
                "destination_adcode": "110000",
                "end_date": base.start_date + timedelta(days=1),
                "pace": TripPace.RELAXED,
                "constraints": ConstraintSet(),
            }
        )
        pipeline = FixtureProductPlanningPipeline(request)
        specialists = await pipeline.run_specialists(request, data_mode=DataMode.FIXTURE)
        materials = await pipeline.build_materials(specialists)
        plan_result = pipeline.run_plan(request, materials)
        assert plan_result.plan is not None
        plan = plan_result.plan
        target_day = plan.days[1]
        replaced_item = target_day.items[0]
        explore = next(
            branch.explore_result
            for branch in specialists.branches
            if branch.specialist == SpecialistName.EXPLORE
        )
        assert explore is not None
        scheduled_ids = {item.candidate_id for item in materials.shortlist.poi_candidates}
        replacement = next(
            item.candidate
            for item in explore.observations
            if item.candidate.candidate_id not in scheduled_ids
            and not is_meal_candidate(item.candidate)
        )
        revision = PlanRevisionRequest(
            revision_id="revision-replace-one-observed-activity-v1",
            base_version_id="plan-version-fixture-v1",
            base_plan_id=plan.plan_id,
            target_date=target_day.date,
            operation=PlanRevisionOperation.REPLACE_ACTIVITY,
            replaced_item_id=replaced_item.item_id,
            replacement_candidate_id=replacement.candidate_id,
            target_item_ids=tuple(item.item_id for item in target_day.items),
            protected_item_ids=tuple(item.item_id for item in plan.days[0].items),
            confirmed=True,
        )

        revised = await apply_activity_replacement(
            request,
            plan,
            materials,
            revision,
            pipeline.get_revision_route,
        )

        assert revised.revised_plan.days[0] == plan.days[0]
        assert revised.revised_plan.days[1] != plan.days[1]
        assert revised.diff.changed_dates == (target_day.date,)
        assert revised.diff.removed_item_ids == (replaced_item.item_id,)
        assert len(revised.diff.added_item_ids) == 1
        assert revised.provider_call_count == len(target_day.items)
        assert revised.model_call_count == 0
        assert revised.reused_provider_results is False
        assert revised.revised_materials is not None
        assert revised.revised_materials.activity_replacement is not None
        replacement_item = next(
            item
            for item in revised.revised_plan.days[1].items
            if item.item_id == revised.diff.added_item_ids[0]
        )
        assert replacement_item.kind == ActivityKind.ATTRACTION
        assert replacement_item.candidate_id == replacement.candidate_id
        assert replacement_item.title == replacement.name
        assert replacement_item.source == replacement.source

    asyncio.run(scenario())


def test_activity_replacement_batch_replaces_the_whole_target_day_atomically() -> None:
    async def scenario() -> None:
        base = load_vertical_slice_suite().cases[0].request
        request = TripRequest.model_validate(
            {
                **base.model_dump(mode="python"),
                "request_id": "revision-replace-weather-day-fixture",
                "destination_adcode": "110000",
                "end_date": base.start_date + timedelta(days=1),
                "pace": TripPace.RELAXED,
                "constraints": ConstraintSet(),
            }
        )
        pipeline = FixtureProductPlanningPipeline(request)
        specialists = await pipeline.run_specialists(request, data_mode=DataMode.FIXTURE)
        materials = await pipeline.build_materials(specialists)
        plan_result = pipeline.run_plan(request, materials)
        assert plan_result.plan is not None
        plan = plan_result.plan
        target_day = plan.days[0]
        protected_day = plan.days[1]
        assert len(target_day.items) == 2

        explore = next(
            branch.explore_result
            for branch in specialists.branches
            if branch.specialist == SpecialistName.EXPLORE
        )
        assert explore is not None
        scheduled_ids = {item.candidate_id for item in materials.shortlist.poi_candidates}
        indoor_candidates = tuple(
            item.candidate
            for item in explore.observations
            if item.candidate.candidate_id not in scheduled_ids
            and item.candidate.environment == ActivityEnvironment.INDOOR
            and not is_meal_candidate(item.candidate)
        )
        assert len(indoor_candidates) >= len(target_day.items)
        selected = indoor_candidates[: len(target_day.items)]
        revision = PlanRevisionRequest(
            revision_id="revision-replace-weather-day-v1",
            base_version_id="plan-version-fixture-v1",
            base_plan_id=plan.plan_id,
            target_date=target_day.date,
            operation=PlanRevisionOperation.REPLACE_ACTIVITY,
            activity_replacements=tuple(
                {
                    "replaced_item_id": item.item_id,
                    "replacement_candidate_id": candidate.candidate_id,
                }
                for item, candidate in zip(target_day.items, selected, strict=True)
            ),
            target_item_ids=tuple(item.item_id for item in target_day.items),
            protected_item_ids=tuple(item.item_id for item in protected_day.items),
            confirmed=True,
        )

        revised = await apply_activity_replacement(
            request,
            plan,
            materials,
            revision,
            pipeline.get_revision_route,
        )

        assert revised.executor_version == "deterministic-local-revision-v3"
        assert revised.diff.changed_dates == (target_day.date,)
        assert revised.diff.removed_item_ids == tuple(item.item_id for item in target_day.items)
        assert len(revised.diff.added_item_ids) == len(target_day.items)
        assert revised.provider_call_count == len(target_day.items)
        assert revised.model_call_count == 0
        assert revised.revised_plan.days[1] == protected_day
        assert {item.candidate_id for item in revised.revised_plan.days[0].items} == {
            item.candidate_id for item in selected
        }
        assert revised.revised_materials is not None
        assert revised.revised_materials.activity_replacement is None
        assert len(revised.revised_materials.activity_replacements) == len(target_day.items)

    asyncio.run(scenario())


def test_activity_replacement_batch_rejects_duplicate_targets_or_candidates() -> None:
    _, result = _normal_case_result()
    target_day = result.plan.days[0]
    scope = {
        "revision_id": "revision-invalid-weather-day-v1",
        "base_version_id": "plan-version-fixture-v1",
        "base_plan_id": result.plan.plan_id,
        "target_date": target_day.date,
        "operation": PlanRevisionOperation.REPLACE_ACTIVITY,
        "target_item_ids": [item.item_id for item in target_day.items],
        "protected_item_ids": [item.item_id for day in result.plan.days[1:] for item in day.items],
        "confirmed": True,
    }
    first_item_id = target_day.items[0].item_id
    second_item_id = "plan-item-second-target"
    scope["target_item_ids"] = [first_item_id, second_item_id]
    with pytest.raises(ValidationError, match="target ids must be unique"):
        PlanRevisionRequest.model_validate(
            {
                **scope,
                "activity_replacements": [
                    {
                        "replaced_item_id": first_item_id,
                        "replacement_candidate_id": "candidate-indoor-one",
                    },
                    {
                        "replaced_item_id": first_item_id,
                        "replacement_candidate_id": "candidate-indoor-two",
                    },
                ],
            }
        )
    with pytest.raises(ValidationError, match="candidate ids must be unique"):
        PlanRevisionRequest.model_validate(
            {
                **scope,
                "activity_replacements": [
                    {
                        "replaced_item_id": first_item_id,
                        "replacement_candidate_id": "candidate-indoor-one",
                    },
                    {
                        "replaced_item_id": second_item_id,
                        "replacement_candidate_id": "candidate-indoor-one",
                    },
                ],
            }
        )
