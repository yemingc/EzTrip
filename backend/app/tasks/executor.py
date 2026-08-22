from pathlib import Path

from app.agents.contracts import (
    PlannerModelResponse,
    PlannerPlacementProposal,
    PlannerProposalBatch,
)
from app.agents.single_planner import DeepSeekPlannerProposalModel
from app.core.config import Settings
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerContext
from app.domain.sources import DataMode
from app.planning.stateful_contracts import StatefulPlanningSnapshot
from app.planning.stateful_graph import open_sqlite_planning_runtime
from app.providers import load_fixture_amap_provider, open_live_amap_provider
from app.tasks.contracts import PlanningTaskSubmission
from app.tasks.service import PlanningProgressEmitter, PlanningTaskConfigurationError


class FixtureTaskPlannerProposalModel:
    """Deterministic scheduler for the allow-listed offline AMap fixture only."""

    _slots = ("09:00", "14:00", "18:00")

    def propose(
        self,
        context: PlannerContext,
        candidates: tuple[CandidatePOI, ...],
    ) -> PlannerModelResponse:
        day_count = len(context.days)
        if len(candidates) < day_count or len(candidates) > day_count * len(self._slots):
            raise PlanningTaskConfigurationError(
                "fixture planning requires one to three candidates per trip day"
            )
        proposals = tuple(
            PlannerPlacementProposal(
                candidate_id=candidate.candidate_id,
                day_number=(index % day_count) + 1,
                start_time=self._slots[index // day_count],
                reason="离线 fixture 使用稳定轮转排程, 仅用于协议与产品流程验证。",
            )
            for index, candidate in enumerate(candidates)
        )
        return PlannerModelResponse(
            proposal=PlannerProposalBatch(items=proposals),
            model="fixture-task-planner-v1",
            latency_ms=0,
        )


class StatefulGraphPlanningTaskExecutor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def execute(
        self,
        submission: PlanningTaskSubmission,
        emit_progress: PlanningProgressEmitter,
    ) -> StatefulPlanningSnapshot:
        checkpoint_path = (
            Path(self._settings.planning_checkpoint_dir) / f"{submission.task_id}.sqlite"
        )
        if submission.data_mode == DataMode.FIXTURE:
            provider = load_fixture_amap_provider()
            model = FixtureTaskPlannerProposalModel()
            async with open_sqlite_planning_runtime(
                checkpoint_path,
                provider,
                model,
            ) as runtime:
                return await runtime.start_with_progress(
                    submission.task_id,
                    submission.request,
                    submission.cost_items,
                    data_mode=DataMode.FIXTURE,
                    on_progress=emit_progress,
                )

        if not self._settings.planning_live_enabled:
            raise PlanningTaskConfigurationError(
                "live task execution requires EZTRIP_PLANNING_LIVE_ENABLED=true"
            )
        live_model = DeepSeekPlannerProposalModel(self._settings)
        async with (
            open_live_amap_provider(self._settings) as provider,
            open_sqlite_planning_runtime(
                checkpoint_path,
                provider,
                live_model,
            ) as runtime,
        ):
            return await runtime.start_with_progress(
                submission.task_id,
                submission.request,
                submission.cost_items,
                data_mode=DataMode.LIVE,
                on_progress=emit_progress,
            )
