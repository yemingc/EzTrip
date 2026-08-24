import asyncio
import json
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import SecretStr

from app.agents import plan_agent as plan_agent_module
from app.agents.contracts import (
    ModelTokenUsage,
    PlannerModelResponse,
    PlannerPlacementProposal,
    PlannerProposalBatch,
)
from app.agents.plan_agent import (
    PLAN_AGENT_TOOL_NAME,
    PLAN_AGENT_TOOL_SCHEMA,
    DeepSeekPlanProposalModel,
    PlanAgentConfigurationError,
    PlanAgentProtocolError,
    planning_material_sha256,
    run_live_plan_agent,
    run_plan_agent,
)
from app.agents.plan_agent_contracts import PlanAgentRunStatus
from app.core.config import Settings
from app.domain.money import BudgetCategory
from app.domain.request import BudgetConstraint, TripPace, TripRequest
from app.domain.sources import DataMode
from app.domain.validation import PlanValidationStatus
from app.evaluation.planning_materials import (
    PlanningMaterialFixtureExploreModel,
    PlanningMaterialFixtureStayModel,
    PlanningMaterialRouteProvider,
)
from app.evaluation.planning_materials_contracts import RouteFailureInjection
from app.evaluation.specialist_fanout import (
    build_specialist_scenario_provider,
    load_specialist_fanout_suite,
)
from app.planning.material_builder import build_planning_material_bundle
from app.planning.material_contracts import (
    PlanningMaterialBundle,
    PlanningMaterialIssueCode,
    PlanningMaterialStatus,
)
from app.planning.specialist_fanout import run_specialist_fanout


class FixedPlanModel:
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, materials: PlanningMaterialBundle) -> PlannerModelResponse:
        self.calls += 1
        placements = (
            (1, "09:00"),
            (1, "14:00"),
            (2, "09:00"),
            (2, "14:00"),
        )
        return PlannerModelResponse(
            proposal=PlannerProposalBatch(
                items=tuple(
                    PlannerPlacementProposal(
                        candidate_id=candidate.candidate_id,
                        day_number=placements[index][0],
                        start_time=placements[index][1],
                        reason="依据路线、天气与同行人材料进行固定夹具排程。",
                    )
                    for index, candidate in enumerate(materials.shortlist.poi_candidates)
                )
            ),
            model="fixture-plan-agent-model",
            latency_ms=25,
            usage=ModelTokenUsage(
                prompt_tokens=200,
                completion_tokens=40,
                total_tokens=240,
            ),
        )


class FailIfCalledPlanModel:
    def propose(self, materials: PlanningMaterialBundle) -> PlannerModelResponse:
        raise AssertionError(f"Plan model should not run for {materials.status.value} materials")


class InvalidCandidatePlanModel(FixedPlanModel):
    def propose(self, materials: PlanningMaterialBundle) -> PlannerModelResponse:
        response = super().propose(materials)
        proposals = list(response.proposal.items)
        proposals[0] = proposals[0].model_copy(update={"candidate_id": "invented-poi"})
        return response.model_copy(
            update={"proposal": PlannerProposalBatch(items=tuple(proposals))}
        )


class FakeCompletions:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeOpenAIClient:
    def __init__(self, responses: list[object]) -> None:
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeLangSmithClient:
    def __init__(self) -> None:
        self.flush_timeouts: list[float] = []

    def flush(self, timeout: float) -> None:
        self.flush_timeouts.append(timeout)


def _settings(*, tracing: bool = True, with_deepseek_key: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key=(SecretStr("deepseek-test-secret-value") if with_deepseek_key else None),
        langsmith_api_key=SecretStr("langsmith-test-secret-value"),
        langsmith_tracing=tracing,
    )


def _tool_call_response(
    *,
    arguments: str,
    call_count: int = 1,
    tool_name: str = PLAN_AGENT_TOOL_NAME,
    with_usage: bool = True,
) -> object:
    tool_calls = [
        SimpleNamespace(
            id=f"call_plan_agent_{index}",
            type="function",
            function=SimpleNamespace(name=tool_name, arguments=arguments),
        )
        for index in range(call_count)
    ]
    usage = (
        SimpleNamespace(prompt_tokens=190, completion_tokens=35, total_tokens=225)
        if with_usage
        else None
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls, content=None))],
        usage=usage,
    )


def _specialist_case() -> Any:
    return next(
        item
        for item in load_specialist_fanout_suite().cases
        if item.case_id == "specialist-fanout-complete-v1"
    )


def _request_with_soft_budget(request: TripRequest) -> TripRequest:
    payload = request.model_dump(mode="json")
    payload["budget"] = BudgetConstraint(
        total_limit=Decimal("3000.00"),
        included_categories=tuple(BudgetCategory),
        hard_limit=False,
    ).model_dump(mode="json")
    return TripRequest.model_validate(payload)


async def _materials(
    failure: RouteFailureInjection = RouteFailureInjection.NONE,
) -> tuple[TripRequest, PlanningMaterialBundle]:
    case = _specialist_case()
    request = _request_with_soft_budget(case.request)
    specialist_result = await run_specialist_fanout(
        request,
        build_specialist_scenario_provider(case),
        PlanningMaterialFixtureExploreModel(),
        PlanningMaterialFixtureStayModel(),
        data_mode=DataMode.FIXTURE,
    )
    materials = await build_planning_material_bundle(
        specialist_result,
        PlanningMaterialRouteProvider(failure),
    )
    return request, materials


def test_plan_agent_builds_grounded_trip_plan_from_ready_multi_agent_materials() -> None:
    async def exercise() -> None:
        request, materials = await _materials()
        model = FixedPlanModel()

        result = run_plan_agent(request, materials, model)

        assert materials.status == PlanningMaterialStatus.READY
        assert result.status == PlanAgentRunStatus.PLANNED
        assert result.model_call_count == model.calls == 1
        assert result.usage is not None and result.usage.total_tokens == 240
        assert result.plan is not None
        assert result.validation is not None
        assert result.validation.status == PlanValidationStatus.WARNING
        assert tuple(day.date for day in result.plan.days) == tuple(
            day.date for day in materials.planner_context.days
        )
        assert tuple(item.proposal.candidate_id for item in result.decisions) == tuple(
            item.candidate_id for item in materials.shortlist.poi_candidates
        )
        assert all(item.item.source is not None for item in result.decisions)
        assert all(item.item.route_from_previous is not None for item in result.decisions)
        assert result.route_edge_ids_used == tuple(item.route_edge_id for item in result.decisions)
        assert result.plan.cost_items == ()
        assert tuple(item.risk_id for item in result.plan.weather_risks) == (
            f"weather-{_specialist_case().case_id}",
        )
        assert result.plan.days[0].weather_risk_ids == result.input_weather_risk_ids
        assert result.plan.days[-1].weather_risk_ids == ()
        assert result.plan.days[-1].items[0].kind.value == "free_time"

        stay = materials.shortlist.primary_stay
        assert stay is not None
        stay_id = stay.candidate_id
        first_day_route = result.decisions[0].item.route_from_previous
        second_day_route = result.decisions[2].item.route_from_previous
        assert first_day_route is not None and first_day_route.origin.candidate_id == stay_id
        assert second_day_route is not None and second_day_route.origin.candidate_id == stay_id

    asyncio.run(exercise())


def test_plan_agent_excludes_weather_risks_outside_the_trip_dates() -> None:
    async def exercise() -> None:
        request, materials = await _materials()
        weather_branch = next(
            branch for branch in materials.specialist_result.branches if branch.weather_risks
        )
        original_risk = weather_branch.weather_risks[0]
        shifted_risk = original_risk.model_copy(
            update={
                "starts_at": original_risk.starts_at + timedelta(days=30),
                "ends_at": original_risk.ends_at + timedelta(days=30),
            }
        )
        shifted_branch = weather_branch.model_copy(update={"weather_risks": (shifted_risk,)})
        shifted_specialists = materials.specialist_result.model_copy(
            update={
                "branches": tuple(
                    shifted_branch if branch is weather_branch else branch
                    for branch in materials.specialist_result.branches
                )
            }
        )
        shifted_materials = materials.model_copy(update={"specialist_result": shifted_specialists})

        result = run_plan_agent(request, shifted_materials, FixedPlanModel())

        assert result.plan is not None
        assert result.plan.weather_risks == ()
        assert result.input_weather_risk_ids == ()
        assert all(day.weather_risk_ids == () for day in result.plan.days)

    asyncio.run(exercise())


def test_partial_route_materials_produce_a_grounded_draft_with_explicit_gap() -> None:
    async def exercise() -> None:
        request, materials = await _materials(RouteFailureInjection.ONE_TIMEOUT)
        model = FixedPlanModel()

        result = run_plan_agent(request, materials, model)

        assert materials.status == PlanningMaterialStatus.PARTIAL
        assert result.status == PlanAgentRunStatus.PLANNED
        assert result.skip_reason is None
        assert result.model_call_count == model.calls == 1
        assert result.plan is not None
        assert result.validation is not None
        assert len(result.decisions) == len(materials.shortlist.poi_candidates)
        assert any(item.item.route_from_previous is None for item in result.decisions)
        assert len(result.route_edge_ids_used) < len(result.decisions)

    asyncio.run(exercise())


def test_activity_coverage_gap_produces_an_editable_draft_instead_of_skipping() -> None:
    async def exercise() -> None:
        case = _specialist_case()
        request = _request_with_soft_budget(
            case.request.model_copy(
                update={
                    "end_date": case.request.start_date + timedelta(days=1),
                    "pace": TripPace.RELAXED,
                }
            )
        )
        specialist_result = await run_specialist_fanout(
            request,
            build_specialist_scenario_provider(case),
            PlanningMaterialFixtureExploreModel(),
            PlanningMaterialFixtureStayModel(),
            data_mode=DataMode.FIXTURE,
        )
        materials = await build_planning_material_bundle(
            specialist_result,
            PlanningMaterialRouteProvider(RouteFailureInjection.NONE),
        )

        result = run_plan_agent(request, materials, FixedPlanModel())

        assert materials.status == PlanningMaterialStatus.PARTIAL
        assert PlanningMaterialIssueCode.ACTIVITY_COVERAGE_INSUFFICIENT in materials.issues
        assert len(materials.shortlist.poi_candidates) == 3
        assert result.status == PlanAgentRunStatus.PLANNED
        assert result.plan is not None
        assert len(result.decisions) == 3

    asyncio.run(exercise())


def test_plan_agent_rejects_an_invented_candidate_before_plan_assembly() -> None:
    async def exercise() -> None:
        request, materials = await _materials()

        with pytest.raises(PlanAgentProtocolError, match="every shortlist POI"):
            run_plan_agent(request, materials, InvalidCandidatePlanModel())

    asyncio.run(exercise())


def test_plan_agent_rejects_request_material_identity_mismatch() -> None:
    async def exercise() -> None:
        request, materials = await _materials()
        changed = request.model_copy(update={"raw_text": f"{request.raw_text} changed"})

        with pytest.raises(PlanAgentProtocolError, match="does not match"):
            run_plan_agent(changed, materials, FailIfCalledPlanModel())

    asyncio.run(exercise())


def test_plan_agent_material_hash_and_output_schema_are_deterministic() -> None:
    async def exercise() -> None:
        _, materials = await _materials()

        assert planning_material_sha256(materials) == planning_material_sha256(
            PlanningMaterialBundle.model_validate(materials.model_dump(mode="json"))
        )
        function = cast(dict[str, Any], PLAN_AGENT_TOOL_SCHEMA["function"])
        parameters = cast(dict[str, Any], function["parameters"])
        item_properties = parameters["$defs"]["PlannerPlacementProposal"]["properties"]
        assert set(item_properties) == {"candidate_id", "day_number", "start_time", "reason"}
        assert "title" not in item_properties
        assert "source" not in item_properties
        assert "price" not in item_properties

    asyncio.run(exercise())


def test_plan_agent_replay_ignores_material_latency_telemetry_for_stable_ids() -> None:
    async def exercise() -> None:
        request, materials = await _materials()
        replay_materials = materials.model_copy(
            update={
                "route_matrix": materials.route_matrix.model_copy(
                    update={"latency_ms": materials.route_matrix.latency_ms + 999}
                )
            }
        )

        first = run_plan_agent(request, materials, FixedPlanModel())
        replay = run_plan_agent(request, replay_materials, FixedPlanModel())

        assert first.input_material_sha256 == replay.input_material_sha256
        assert first.plan is not None and replay.plan is not None
        assert first.plan.plan_id == replay.plan.plan_id

    asyncio.run(exercise())


def test_deepseek_plan_adapter_forces_minimal_schema_and_sends_material_boundaries() -> None:
    async def exercise() -> None:
        _, materials = await _materials()
        proposal = FixedPlanModel().propose(materials).proposal
        fake_client = FakeOpenAIClient([_tool_call_response(arguments=proposal.model_dump_json())])
        model = DeepSeekPlanProposalModel(_settings())
        model._client = fake_client  # type: ignore[assignment]

        response = model.propose(materials)

        assert response.proposal == proposal
        assert response.usage is not None and response.usage.total_tokens == 225
        call = fake_client.completions.calls[0]
        assert call["tool_choice"]["function"]["name"] == PLAN_AGENT_TOOL_NAME
        user_payload = json.loads(call["messages"][1]["content"])
        assert user_payload["allowed_poi_candidate_ids"] == [
            item.candidate_id for item in materials.shortlist.poi_candidates
        ]
        assert user_payload["budget"]["semantics"] == ("planning_targets_not_verified_prices")
        assert user_payload["truth_boundaries"] == {
            "opening_hours_verified": False,
            "stay_availability_verified": False,
            "booking_supported": False,
            "budget_targets_are_prices": False,
        }
        assert "raw_text" not in call["messages"][1]["content"]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_tool_call_response(arguments="{}", call_count=2), "exactly one"),
        (
            _tool_call_response(arguments="{}", tool_name="unexpected_plan_tool"),
            "unexpected",
        ),
        (_tool_call_response(arguments="{}"), "invalid Plan Agent arguments"),
    ],
)
def test_deepseek_plan_adapter_rejects_bad_tool_protocol(
    response: object,
    message: str,
) -> None:
    async def exercise() -> None:
        _, materials = await _materials()
        model = DeepSeekPlanProposalModel(_settings())
        model._client = FakeOpenAIClient([response])  # type: ignore[assignment]

        with pytest.raises(PlanAgentProtocolError, match=message):
            model.propose(materials)

    asyncio.run(exercise())


def test_deepseek_plan_adapter_requires_api_key() -> None:
    with pytest.raises(PlanAgentConfigurationError, match="DEEPSEEK_API_KEY"):
        DeepSeekPlanProposalModel(_settings(with_deepseek_key=False))


def test_live_plan_agent_requires_tracing_for_ready_and_partial_drafts() -> None:
    async def exercise() -> None:
        request, ready = await _materials()
        with pytest.raises(PlanAgentConfigurationError, match="LANGSMITH_TRACING"):
            run_live_plan_agent(request, ready, _settings(tracing=False))

        request, partial = await _materials(RouteFailureInjection.ONE_TIMEOUT)
        with pytest.raises(PlanAgentConfigurationError, match="LANGSMITH_TRACING"):
            run_live_plan_agent(request, partial, _settings(tracing=False))

    asyncio.run(exercise())


def test_live_plan_agent_flushes_langsmith(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        request, materials = await _materials()
        fake_langsmith = FakeLangSmithClient()
        model = FixedPlanModel()

        monkeypatch.setattr(
            plan_agent_module,
            "build_langsmith_client",
            lambda *_: fake_langsmith,
        )
        monkeypatch.setattr(
            plan_agent_module,
            "DeepSeekPlanProposalModel",
            lambda *_: model,
        )
        monkeypatch.setattr(
            plan_agent_module,
            "tracing_context",
            lambda **_: nullcontext(),
        )

        result = run_live_plan_agent(request, materials, _settings())

        assert result.model == "fixture-plan-agent-model"
        assert model.calls == 1
        assert fake_langsmith.flush_timeouts == [15.0]

    asyncio.run(exercise())
