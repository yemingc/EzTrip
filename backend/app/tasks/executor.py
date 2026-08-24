from pathlib import Path
from typing import Protocol

from app.agents.contracts import ExploreAgentResult, StayAgentResult
from app.agents.explore_agent import run_live_explore_agent
from app.agents.plan_agent import run_live_plan_agent
from app.agents.plan_agent_contracts import PlanAgentRunResult
from app.agents.stay_agent import run_live_stay_agent
from app.core.config import Settings
from app.destinations import DestinationResolutionService
from app.domain.context import PlannerContext
from app.domain.opening_hours import OpeningHoursEvidenceBundle
from app.domain.planning import TripPlan
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.planning.material_builder import build_planning_material_bundle
from app.planning.material_contracts import PlanningMaterialBundle
from app.planning.product_contracts import ProductPlanningSnapshot
from app.planning.product_graph import ProductPlanningProtocolError, open_sqlite_product_runtime
from app.planning.specialist_contracts import SpecialistFanoutResult
from app.planning.specialist_fanout import run_live_specialist_fanout
from app.planning.stateful_contracts import HumanReviewResume
from app.providers import open_live_amap_provider
from app.providers.ports import RouteProvider, SpecialistProvider
from app.tasks.contracts import PlanningTaskSubmission
from app.tasks.product_fixture import FixtureProductPlanningPipeline
from app.tasks.service import PlanningProgressEmitter, PlanningTaskConfigurationError


class LiveProductProvider(SpecialistProvider, RouteProvider, Protocol):
    pass


class LiveProductPlanningPipeline:
    def __init__(self, settings: Settings, provider: LiveProductProvider) -> None:
        self._settings = settings
        self._provider = provider

    async def run_specialists(
        self,
        request: TripRequest,
        *,
        data_mode: DataMode,
    ) -> SpecialistFanoutResult:
        return await run_live_specialist_fanout(
            request,
            self._provider,
            self._settings,
            data_mode=data_mode,
        )

    async def build_materials(
        self,
        specialist_result: SpecialistFanoutResult,
    ) -> PlanningMaterialBundle:
        return await build_planning_material_bundle(specialist_result, self._provider)

    async def rerun_explore(self, context: PlannerContext) -> ExploreAgentResult:
        return await run_live_explore_agent(context, self._provider, self._settings)

    async def rerun_stay(self, context: PlannerContext) -> StayAgentResult:
        return await run_live_stay_agent(context, self._provider, self._settings)

    def run_plan(
        self,
        request: TripRequest,
        materials: PlanningMaterialBundle,
    ) -> PlanAgentRunResult:
        return run_live_plan_agent(request, materials, self._settings)

    def build_opening_hours(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        data_mode: DataMode,
    ) -> OpeningHoursEvidenceBundle:
        del plan
        # AMap V1 does not expose stable typed opening-hours evidence. The empty
        # bundle makes Hard Validator surface that uncertainty instead of inventing it.
        return OpeningHoursEvidenceBundle(
            request_id=request.request_id,
            data_mode=data_mode,
            items=(),
        )


class ResumeOnlyProductPipeline:
    """Fails if checkpoint resume unexpectedly replays a paid or external stage."""

    @staticmethod
    def _unexpected() -> ProductPlanningProtocolError:
        return ProductPlanningProtocolError(
            "checkpoint resume unexpectedly replayed a product planning stage"
        )

    async def run_specialists(
        self,
        request: TripRequest,
        *,
        data_mode: DataMode,
    ) -> SpecialistFanoutResult:
        del request, data_mode
        raise self._unexpected()

    async def build_materials(
        self,
        specialist_result: SpecialistFanoutResult,
    ) -> PlanningMaterialBundle:
        del specialist_result
        raise self._unexpected()

    async def rerun_explore(self, context: PlannerContext) -> ExploreAgentResult:
        del context
        raise self._unexpected()

    async def rerun_stay(self, context: PlannerContext) -> StayAgentResult:
        del context
        raise self._unexpected()

    def run_plan(
        self,
        request: TripRequest,
        materials: PlanningMaterialBundle,
    ) -> PlanAgentRunResult:
        del request, materials
        raise self._unexpected()

    def build_opening_hours(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        data_mode: DataMode,
    ) -> OpeningHoursEvidenceBundle:
        del request, plan, data_mode
        raise self._unexpected()


class ProductGraphPlanningTaskExecutor:
    def __init__(
        self,
        settings: Settings,
        *,
        destination_resolution_service: DestinationResolutionService | None = None,
    ) -> None:
        self._settings = settings
        self._destination_resolution_service = (
            destination_resolution_service or DestinationResolutionService(settings)
        )

    async def execute(
        self,
        submission: PlanningTaskSubmission,
        emit_progress: PlanningProgressEmitter,
    ) -> ProductPlanningSnapshot:
        checkpoint_path = (
            Path(self._settings.planning_checkpoint_dir) / f"{submission.task_id}.sqlite"
        )
        selected_destination = await self._destination_resolution_service.resolve_and_select(
            submission.request.destination_city,
            data_mode=submission.data_mode,
            selected_adcode=submission.selected_destination_adcode,
        )
        request_payload = submission.request.model_dump(mode="python")
        request_payload.update(
            destination_city=selected_destination.planning_city_name,
            destination_adcode=selected_destination.administrative_code,
        )
        resolved_request = TripRequest.model_validate(request_payload)
        if submission.data_mode == DataMode.FIXTURE:
            fixture_pipeline = FixtureProductPlanningPipeline(resolved_request)
            async with open_sqlite_product_runtime(checkpoint_path, fixture_pipeline) as runtime:
                return await runtime.start_with_progress(
                    submission.task_id,
                    resolved_request,
                    submission.cost_items,
                    data_mode=DataMode.FIXTURE,
                    on_progress=emit_progress,
                )

        if not self._settings.planning_live_enabled:
            raise PlanningTaskConfigurationError(
                "live task execution requires EZTRIP_PLANNING_LIVE_ENABLED=true"
            )
        async with open_live_amap_provider(self._settings) as provider:
            live_pipeline = LiveProductPlanningPipeline(self._settings, provider)
            async with open_sqlite_product_runtime(checkpoint_path, live_pipeline) as runtime:
                return await runtime.start_with_progress(
                    submission.task_id,
                    resolved_request,
                    submission.cost_items,
                    data_mode=DataMode.LIVE,
                    on_progress=emit_progress,
                )

    async def resume(
        self,
        task_id: str,
        resume: HumanReviewResume,
        emit_progress: PlanningProgressEmitter,
    ) -> ProductPlanningSnapshot:
        checkpoint_path = Path(self._settings.planning_checkpoint_dir) / f"{task_id}.sqlite"
        if not checkpoint_path.exists():
            raise PlanningTaskConfigurationError("planning checkpoint file does not exist")
        async with open_sqlite_product_runtime(
            checkpoint_path,
            ResumeOnlyProductPipeline(),
        ) as runtime:
            return await runtime.resume_with_progress(
                task_id,
                resume,
                on_progress=emit_progress,
            )


# Compatibility import for callers created before Product Graph V2.
StatefulGraphPlanningTaskExecutor = ProductGraphPlanningTaskExecutor

__all__ = [
    "ProductGraphPlanningTaskExecutor",
    "StatefulGraphPlanningTaskExecutor",
]
