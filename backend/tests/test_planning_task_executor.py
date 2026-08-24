import asyncio
from contextlib import asynccontextmanager
from typing import cast

import pytest

from app.agents.contracts import ExploreAgentResult, StayAgentResult
from app.core.config import Settings
from app.domain.context import PlannerContext
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.planning.material_contracts import PlanningMaterialBundle
from app.planning.specialist_contracts import SpecialistFanoutResult
from app.tasks import executor as executor_module
from app.tasks.executor import LiveProductPlanningPipeline


def test_live_pipeline_uses_a_fresh_amap_session_for_each_provider_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle: list[tuple[str, object]] = []
    stage_providers: list[object] = []

    @asynccontextmanager
    async def open_provider(settings: Settings):
        del settings
        provider = object()
        lifecycle.append(("open", provider))
        try:
            yield provider
        finally:
            lifecycle.append(("close", provider))

    async def run_specialists(
        request: TripRequest,
        provider: object,
        settings: Settings,
        *,
        data_mode: DataMode,
    ) -> SpecialistFanoutResult:
        del request, settings
        assert data_mode == DataMode.LIVE
        stage_providers.append(provider)
        return cast(SpecialistFanoutResult, object())

    async def build_materials(
        result: SpecialistFanoutResult,
        provider: object,
    ) -> PlanningMaterialBundle:
        del result
        stage_providers.append(provider)
        return cast(PlanningMaterialBundle, object())

    async def rerun_explore(
        context: PlannerContext,
        provider: object,
        settings: Settings,
    ) -> ExploreAgentResult:
        del context, settings
        stage_providers.append(provider)
        return cast(ExploreAgentResult, object())

    async def rerun_stay(
        context: PlannerContext,
        provider: object,
        settings: Settings,
    ) -> StayAgentResult:
        del context, settings
        stage_providers.append(provider)
        return cast(StayAgentResult, object())

    monkeypatch.setattr(executor_module, "open_live_amap_provider", open_provider)
    monkeypatch.setattr(executor_module, "run_live_specialist_fanout", run_specialists)
    monkeypatch.setattr(executor_module, "build_planning_material_bundle", build_materials)
    monkeypatch.setattr(executor_module, "run_live_explore_agent", rerun_explore)
    monkeypatch.setattr(executor_module, "run_live_stay_agent", rerun_stay)

    async def exercise_pipeline() -> None:
        pipeline = LiveProductPlanningPipeline(Settings())
        request = cast(TripRequest, object())
        context = cast(PlannerContext, object())
        specialists = await pipeline.run_specialists(request, data_mode=DataMode.LIVE)
        await pipeline.build_materials(specialists)
        await pipeline.rerun_explore(context)
        await pipeline.rerun_stay(context)

    asyncio.run(exercise_pipeline())

    assert len(stage_providers) == 4
    assert len({id(provider) for provider in stage_providers}) == 4
    assert lifecycle == [
        event for provider in stage_providers for event in (("open", provider), ("close", provider))
    ]
