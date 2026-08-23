import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import ValidationError

from app.agents.contracts import PlannerModelResponse
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerContext
from app.domain.sources import DataMode
from app.domain.travel_data import RouteLeg, WeatherRisk
from app.evaluation import (
    CheckpointHitlReport,
    CheckpointHitlSuite,
    evaluate_checkpoint_hitl_suite,
    load_vertical_slice_suite,
)
from app.evaluation.checkpoint import (
    CHECKPOINT_HITL_REPORT_PATH,
    CHECKPOINT_HITL_SUITE_PATH,
)
from app.evaluation.vertical_slice import (
    FixturePlannerProposalModel,
    VerticalSliceScenarioProvider,
)
from app.planning import (
    DuplicatePlanningThreadError,
    HumanReviewAction,
    HumanReviewKind,
    HumanReviewResume,
    PlanningThreadStatus,
    StatefulPlanningNodeName,
    StatefulPlanningProgress,
    StatefulPlanningProtocolError,
    open_sqlite_planning_runtime,
)
from app.providers import POISearchRequest, RouteRequest, WeatherRiskRequest
from scripts.export_checkpoint_hitl_schemas import (
    REPORT_SCHEMA_PATH,
    SUITE_SCHEMA_PATH,
    build_checkpoint_hitl_report_schema,
    build_checkpoint_hitl_suite_schema,
)

FIXED_REVIEW_TIME = datetime(2026, 8, 21, 9, 30, tzinfo=UTC)


class FailIfCalledProvider:
    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        raise AssertionError(f"provider replayed search_pois for {request.keywords}")

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        raise AssertionError(f"provider replayed weather for {request.city_adcode}")

    async def get_route(self, request: RouteRequest) -> RouteLeg:
        raise AssertionError(f"provider replayed route for {request.city_adcode}")


class FailIfCalledPlannerModel:
    def propose(
        self,
        context: PlannerContext,
        candidates: tuple[CandidatePOI, ...],
    ) -> PlannerModelResponse:
        del context, candidates
        raise AssertionError("planner model was replayed after checkpoint restore")


def build_fixture_dependencies(
    case_index: int,
) -> tuple[
    VerticalSliceScenarioProvider,
    FixturePlannerProposalModel,
]:
    case = load_vertical_slice_suite().cases[case_index]
    return (
        VerticalSliceScenarioProvider(case.provider_responses),
        FixturePlannerProposalModel(
            PlannerModelResponse(
                proposal=case.planner_proposal,
                model=case.planner_model,
                latency_ms=0,
            )
        ),
    )


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_checkpoint_hitl_suite_and_report_match_generated_json_schemas() -> None:
    suite_payload = load_json(CHECKPOINT_HITL_SUITE_PATH)
    report_payload = load_json(CHECKPOINT_HITL_REPORT_PATH)
    committed_suite_schema = load_json(SUITE_SCHEMA_PATH)
    committed_report_schema = load_json(REPORT_SCHEMA_PATH)

    assert committed_suite_schema == build_checkpoint_hitl_suite_schema()
    assert committed_report_schema == build_checkpoint_hitl_report_schema()
    Draft202012Validator.check_schema(committed_suite_schema)
    Draft202012Validator.check_schema(committed_report_schema)
    assert not list(Draft202012Validator(committed_suite_schema).iter_errors(suite_payload))
    assert not list(Draft202012Validator(committed_report_schema).iter_errors(report_payload))
    assert CheckpointHitlSuite.model_validate(suite_payload).suite == "stateful-checkpoint-hitl-v1"
    assert CheckpointHitlReport.model_validate(report_payload).case_count == 2


def test_checkpoint_hitl_report_is_mechanically_replayable() -> None:
    committed = CheckpointHitlReport.model_validate(load_json(CHECKPOINT_HITL_REPORT_PATH))
    replayed = asyncio.run(evaluate_checkpoint_hitl_suite())

    assert committed == replayed
    assert replayed.passed_case_count == 2
    assert replayed.passed_check_count == 20
    assert replayed.runtime_reconstruction_count == 2
    assert replayed.no_expensive_replay_count == 2
    assert replayed.draft_preserved_count == 2


def test_checkpoint_hitl_contract_rejects_policy_and_aggregate_drift() -> None:
    suite_payload = copy.deepcopy(load_json(CHECKPOINT_HITL_SUITE_PATH))
    suite_payload["cases"][0]["action"] = "request_revision"
    with pytest.raises(ValidationError, match="must follow review kind"):
        CheckpointHitlSuite.model_validate(suite_payload)

    report_payload = copy.deepcopy(load_json(CHECKPOINT_HITL_REPORT_PATH))
    report_payload["no_expensive_replay_count"] = 1
    with pytest.raises(ValidationError, match="no_expensive_replay_count"):
        CheckpointHitlReport.model_validate(report_payload)


def test_sqlite_checkpoint_restores_pending_review_without_replaying_planning(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        case_index, case = next(
            (index, item)
            for index, item in enumerate(load_vertical_slice_suite().cases)
            if item.expected.outcome == "ready"
        )
        provider, model = build_fixture_dependencies(case_index)
        checkpoint_path = tmp_path / "normal-review.sqlite"
        thread_id = "checkpoint-normal-v1"

        async with open_sqlite_planning_runtime(
            checkpoint_path,
            provider,
            model,
            clock=lambda: FIXED_REVIEW_TIME,
        ) as runtime:
            paused = await runtime.start(
                thread_id,
                case.request,
                case.cost_items,
                data_mode=DataMode.FIXTURE,
            )
            review = paused.state.review_request
            assert review is not None
            assert paused.state.status == PlanningThreadStatus.AWAITING_HUMAN_REVIEW
            assert paused.next_nodes == (StatefulPlanningNodeName.HUMAN_REVIEW.value,)
            assert review.kind == HumanReviewKind.PLAN_APPROVAL
            assert HumanReviewAction.APPROVE_DRAFT in review.allowed_actions
            assert paused.state.vertical_slice is not None
            original_result = paused.state.vertical_slice
            provider.verify_complete()

        async with open_sqlite_planning_runtime(
            checkpoint_path,
            FailIfCalledProvider(),
            FailIfCalledPlannerModel(),
            clock=lambda: FIXED_REVIEW_TIME,
        ) as restored_runtime:
            restored = await restored_runtime.snapshot(thread_id)
            assert restored.state == paused.state
            progress: list[StatefulPlanningProgress] = []

            async def capture_progress(item: StatefulPlanningProgress) -> None:
                progress.append(item)

            terminal = await restored_runtime.resume_with_progress(
                thread_id,
                HumanReviewResume(
                    review_id=review.review_id,
                    action=HumanReviewAction.APPROVE_DRAFT,
                    reviewer_id="reviewer-fixture",
                    comment="批准该草案进入后续执行前准备。",
                ),
                on_progress=capture_progress,
            )
            history = await restored_runtime.history(thread_id)
            with pytest.raises(StatefulPlanningProtocolError, match="not awaiting"):
                await restored_runtime.resume(
                    thread_id,
                    HumanReviewResume(
                        review_id=review.review_id,
                        action=HumanReviewAction.APPROVE_DRAFT,
                        reviewer_id="reviewer-fixture",
                    ),
                )

        assert terminal.state.status == PlanningThreadStatus.APPROVED_DRAFT
        assert terminal.next_nodes == ()
        assert terminal.state.vertical_slice == original_result
        assert terminal.state.vertical_slice.plan.status == "draft"
        assert terminal.state.review_decision is not None
        assert terminal.state.review_decision.decided_at == FIXED_REVIEW_TIME
        assert [item.node for item in progress] == [
            StatefulPlanningNodeName.HUMAN_REVIEW,
            StatefulPlanningNodeName.APPLY_REVIEW_DECISION,
        ]
        assert [item.state_status for item in progress] == [
            PlanningThreadStatus.REVIEW_DECIDED,
            PlanningThreadStatus.APPROVED_DRAFT,
        ]
        assert tuple(event.node for event in terminal.state.events) == tuple(
            StatefulPlanningNodeName
        )
        assert any(entry.state_status == PlanningThreadStatus.PLANNING for entry in history)
        assert history[-1].state_status == PlanningThreadStatus.APPROVED_DRAFT

    asyncio.run(exercise())


def test_conflicted_plan_rejects_approval_and_preserves_pending_checkpoint(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        suite = load_vertical_slice_suite()
        case_index, case = next(
            (index, item)
            for index, item in enumerate(suite.cases)
            if item.expected.outcome == "conflicted"
        )
        provider, model = build_fixture_dependencies(case_index)
        checkpoint_path = tmp_path / "conflict-review.sqlite"
        thread_id = "checkpoint-conflict-v1"

        async with open_sqlite_planning_runtime(
            checkpoint_path,
            provider,
            model,
            clock=lambda: FIXED_REVIEW_TIME,
        ) as runtime:
            paused = await runtime.start(
                thread_id,
                case.request,
                case.cost_items,
                data_mode=DataMode.FIXTURE,
            )
            review = paused.state.review_request
            assert review is not None
            assert review.kind == HumanReviewKind.CONFLICT_RESOLUTION
            assert review.can_finalize is False
            assert HumanReviewAction.APPROVE_DRAFT not in review.allowed_actions

            with pytest.raises(StatefulPlanningProtocolError, match="not allowed"):
                await runtime.resume(
                    thread_id,
                    HumanReviewResume(
                        review_id=review.review_id,
                        action=HumanReviewAction.APPROVE_DRAFT,
                        reviewer_id="reviewer-fixture",
                    ),
                )
            still_pending = await runtime.snapshot(thread_id)
            assert still_pending.state == paused.state

            terminal = await runtime.resume(
                thread_id,
                HumanReviewResume(
                    review_id=review.review_id,
                    action=HumanReviewAction.ACKNOWLEDGE_CONFLICT,
                    reviewer_id="reviewer-fixture",
                    comment="已知预算硬冲突, 不将草案标记为可执行。",
                ),
            )

        assert terminal.state.status == PlanningThreadStatus.CONFLICT_ACKNOWLEDGED
        assert terminal.state.vertical_slice is not None
        assert terminal.state.vertical_slice.plan.status == "draft"
        assert terminal.state.vertical_slice.validation.can_finalize is False

    asyncio.run(exercise())


def test_start_rejects_duplicate_thread_id(tmp_path: Path) -> None:
    async def exercise() -> None:
        case = load_vertical_slice_suite().cases[0]
        provider, model = build_fixture_dependencies(0)
        async with open_sqlite_planning_runtime(
            tmp_path / "duplicate.sqlite",
            provider,
            model,
        ) as runtime:
            await runtime.start(
                "duplicate-thread-v1",
                case.request,
                case.cost_items,
                data_mode=DataMode.FIXTURE,
            )
            with pytest.raises(DuplicatePlanningThreadError, match="already has checkpoint"):
                await runtime.start(
                    "duplicate-thread-v1",
                    case.request,
                    case.cost_items,
                    data_mode=DataMode.FIXTURE,
                )

    asyncio.run(exercise())


def test_resume_rejects_wrong_review_id_without_consuming_interrupt(tmp_path: Path) -> None:
    async def exercise() -> None:
        case = load_vertical_slice_suite().cases[0]
        provider, model = build_fixture_dependencies(0)
        async with open_sqlite_planning_runtime(
            tmp_path / "wrong-review.sqlite",
            provider,
            model,
        ) as runtime:
            paused = await runtime.start(
                "wrong-review-thread-v1",
                case.request,
                case.cost_items,
                data_mode=DataMode.FIXTURE,
            )
            with pytest.raises(StatefulPlanningProtocolError, match="does not match"):
                await runtime.resume(
                    "wrong-review-thread-v1",
                    HumanReviewResume(
                        review_id="human-review-wrong",
                        action=HumanReviewAction.APPROVE_DRAFT,
                        reviewer_id="reviewer-fixture",
                    ),
                )
            assert (await runtime.snapshot("wrong-review-thread-v1")).state == paused.state

    asyncio.run(exercise())


def test_unknown_thread_has_no_snapshot_or_history(tmp_path: Path) -> None:
    async def exercise() -> None:
        provider, model = build_fixture_dependencies(0)
        async with open_sqlite_planning_runtime(
            tmp_path / "unknown.sqlite",
            provider,
            model,
        ) as runtime:
            with pytest.raises(StatefulPlanningProtocolError, match="does not exist"):
                await runtime.snapshot("unknown-thread-v1")
            with pytest.raises(StatefulPlanningProtocolError, match="has no history"):
                await runtime.history("unknown-thread-v1")

    asyncio.run(exercise())


def test_sqlite_checkpoint_keeps_thread_states_isolated(tmp_path: Path) -> None:
    async def exercise() -> None:
        case = load_vertical_slice_suite().cases[0]
        checkpoint_path = tmp_path / "isolated-threads.sqlite"
        first_provider, first_model = build_fixture_dependencies(0)
        async with open_sqlite_planning_runtime(
            checkpoint_path,
            first_provider,
            first_model,
        ) as first_runtime:
            first = await first_runtime.start(
                "isolated-thread-one-v1",
                case.request,
                case.cost_items,
                data_mode=DataMode.FIXTURE,
            )

        second_provider, second_model = build_fixture_dependencies(0)
        async with open_sqlite_planning_runtime(
            checkpoint_path,
            second_provider,
            second_model,
        ) as second_runtime:
            second = await second_runtime.start(
                "isolated-thread-two-v1",
                case.request,
                case.cost_items,
                data_mode=DataMode.FIXTURE,
            )
            restored_first = await second_runtime.snapshot("isolated-thread-one-v1")

        assert first.state.thread_id == "isolated-thread-one-v1"
        assert second.state.thread_id == "isolated-thread-two-v1"
        assert restored_first.state == first.state
        assert restored_first.checkpoint_id == first.checkpoint_id
        assert second.checkpoint_id != first.checkpoint_id

    asyncio.run(exercise())
