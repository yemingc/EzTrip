import asyncio
import copy
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.agents.contracts import (
    ExploreCandidateSelectionProposal,
    ExploreEvidenceKind,
    ExploreEvidenceReference,
    ExploreQueryKind,
    ExploreQueryModelResponse,
    ExploreQueryProposal,
    ExploreQueryProposalBatch,
    ExploreSelectionModelResponse,
    ExploreSelectionProposalBatch,
    ModelTokenUsage,
    StayCandidateSelectionProposal,
    StayEvidenceKind,
    StayEvidenceReference,
    StayQueryModelResponse,
    StayQueryProposal,
    StayQueryProposalBatch,
    StaySelectionModelResponse,
    StaySelectionProposalBatch,
)
from app.core.config import Settings
from app.domain.candidates import CandidatePOI, CandidateStay
from app.domain.provider import ProviderErrorCategory, ProviderFailure
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import RiskSeverity, WeatherRisk, WeatherRiskType
from app.evaluation import load_explore_agent_suite, load_stay_agent_suite
from app.evaluation.explore import materialize_fixture_candidate
from app.evaluation.stay import materialize_stay_fixture_candidate
from app.planning import specialist_fanout as fanout_module
from app.planning.specialist_contracts import (
    SpecialistBranchStatus,
    SpecialistFailureCategory,
    SpecialistFanoutResult,
    SpecialistFanoutStatus,
    SpecialistName,
    SpecialistSkipReason,
)
from app.planning.specialist_fanout import (
    DuplicateSpecialistThreadError,
    SpecialistFanoutConfigurationError,
    open_sqlite_specialist_runtime,
    run_live_specialist_fanout,
    run_specialist_fanout,
)
from app.providers.errors import ProviderRequestError
from app.providers.ports import POISearchRequest, StaySearchRequest, WeatherRiskRequest


class FixedExploreModel:
    def __init__(self) -> None:
        self.query_calls = 0
        self.selection_calls = 0

    def propose_queries(self, context: Any) -> ExploreQueryModelResponse:
        self.query_calls += 1
        return ExploreQueryModelResponse(
            proposal=ExploreQueryProposalBatch(
                items=(
                    ExploreQueryProposal(
                        kind=ExploreQueryKind.ATTRACTION,
                        keywords=f"{context.destination.normalized_name}历史文化景点",
                        reason="覆盖已表达的历史文化偏好。",
                    ),
                )
            ),
            model="fixture-explore-fanout-model",
            latency_ms=10,
        )

    def select_candidates(
        self,
        context: Any,
        queries: Any,
        observations: Any,
    ) -> ExploreSelectionModelResponse:
        del context, queries
        self.selection_calls += 1
        candidate = observations[0].candidate
        return ExploreSelectionModelResponse(
            proposal=ExploreSelectionProposalBatch(
                items=(
                    ExploreCandidateSelectionProposal(
                        candidate_id=candidate.candidate_id,
                        rank=1,
                        reason="候选类别与旅行偏好相符。",
                        evidence=(
                            ExploreEvidenceReference(
                                kind=ExploreEvidenceKind.CATEGORY,
                                value=candidate.categories[0],
                            ),
                        ),
                    ),
                )
            ),
            model="fixture-explore-fanout-model",
            latency_ms=20,
        )


class FiveQueryExploreModel(FixedExploreModel):
    def propose_queries(self, context: Any) -> ExploreQueryModelResponse:
        self.query_calls += 1
        return ExploreQueryModelResponse(
            proposal=ExploreQueryProposalBatch(
                items=tuple(
                    ExploreQueryProposal(
                        kind=ExploreQueryKind.ATTRACTION,
                        keywords=f"{context.destination.normalized_name}历史文化景点{index}",
                        reason=f"为多日行程补充第 {index} 组主要活动候选。",
                    )
                    for index in range(1, 6)
                )
            ),
            model="fixture-five-query-explore-model",
            latency_ms=10,
        )


class FixedStayModel:
    def __init__(self) -> None:
        self.query_calls = 0
        self.selection_calls = 0

    def propose_queries(self, context: Any) -> StayQueryModelResponse:
        self.query_calls += 1
        return StayQueryModelResponse(
            proposal=StayQueryProposalBatch(
                items=(
                    StayQueryProposal(
                        target_area="中心城区",
                        keywords=f"{context.destination.normalized_name}中心城区住宿",
                        reason="覆盖已表达的住宿区域偏好。",
                    ),
                )
            ),
            model="fixture-stay-fanout-model",
            latency_ms=11,
        )

    def select_candidates(
        self,
        context: Any,
        queries: Any,
        observations: Any,
    ) -> StaySelectionModelResponse:
        del context, queries
        self.selection_calls += 1
        candidate = observations[0].candidate
        return StaySelectionModelResponse(
            proposal=StaySelectionProposalBatch(
                items=(
                    StayCandidateSelectionProposal(
                        candidate_id=candidate.candidate_id,
                        rank=1,
                        reason="候选区域与住宿搜索方向相符。",
                        evidence=(
                            StayEvidenceReference(
                                kind=StayEvidenceKind.AREA_NAME,
                                value=candidate.area_name,
                            ),
                        ),
                    ),
                )
            ),
            model="fixture-stay-fanout-model",
            latency_ms=21,
        )


class UsageExploreModel(FixedExploreModel):
    def propose_queries(self, context: Any) -> ExploreQueryModelResponse:
        response = super().propose_queries(context)
        return response.model_copy(
            update={
                "usage": ModelTokenUsage(
                    prompt_tokens=100,
                    completion_tokens=20,
                    total_tokens=120,
                )
            }
        )


class FailIfCalledExploreModel(FixedExploreModel):
    def propose_queries(self, context: Any) -> ExploreQueryModelResponse:
        raise AssertionError(f"Explore model replayed for {context.request_id}")


class FailIfCalledStayModel(FixedStayModel):
    def propose_queries(self, context: Any) -> StayQueryModelResponse:
        raise AssertionError(f"Stay model replayed for {context.request_id}")


class ConcurrentSpecialistProvider:
    def __init__(
        self,
        poi: CandidatePOI,
        stay: CandidateStay,
        weather_risks: tuple[WeatherRisk, ...],
        *,
        failure: SpecialistName | None = None,
        require_parallel_entry: bool = False,
    ) -> None:
        self.poi = poi
        self.stay = stay
        self.weather_risks = weather_risks
        self.failure = failure
        self.require_parallel_entry = require_parallel_entry
        self.poi_calls = 0
        self.stay_calls = 0
        self.weather_calls = 0
        self._entered: set[SpecialistName] = set()
        self._gate_open = asyncio.Event()
        self._active = 0
        self.peak_active = 0

    async def _gate(self, specialist: SpecialistName) -> None:
        if not self.require_parallel_entry:
            return
        self._active += 1
        self.peak_active = max(self.peak_active, self._active)
        self._entered.add(specialist)
        if self._entered == set(SpecialistName):
            self._gate_open.set()
        await asyncio.wait_for(self._gate_open.wait(), timeout=1.0)
        await asyncio.sleep(0.03)
        self._active -= 1

    def _raise_if_failed(self, specialist: SpecialistName, operation: str) -> None:
        if self.failure != specialist:
            return
        raise ProviderRequestError(
            ProviderFailure(
                provider="specialist-fixture",
                operation=operation,
                category=ProviderErrorCategory.TIMEOUT,
                message=f"injected {specialist.value} timeout",
                retryable=True,
            )
        )

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        self.poi_calls += 1
        await self._gate(SpecialistName.EXPLORE)
        self._raise_if_failed(SpecialistName.EXPLORE, "search_pois")
        return (self.poi,)[: request.limit]

    async def search_stays(self, request: StaySearchRequest) -> tuple[CandidateStay, ...]:
        self.stay_calls += 1
        await self._gate(SpecialistName.STAY)
        self._raise_if_failed(SpecialistName.STAY, "search_stays")
        return (self.stay,)[: request.limit]

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        del request
        self.weather_calls += 1
        await self._gate(SpecialistName.WEATHER)
        self._raise_if_failed(SpecialistName.WEATHER, "get_weather_risks")
        return self.weather_risks


class FailIfCalledProvider(ConcurrentSpecialistProvider):
    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        raise AssertionError(f"Explore provider replayed for {request.keywords}")

    async def search_stays(self, request: StaySearchRequest) -> tuple[CandidateStay, ...]:
        raise AssertionError(f"Stay provider replayed for {request.keywords}")

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        raise AssertionError(f"Weather provider replayed for {request.city_adcode}")


class FakeLangSmithClient:
    def __init__(self) -> None:
        self.flush_timeouts: list[float] = []

    def flush(self, timeout: float) -> None:
        self.flush_timeouts.append(timeout)


def sample_dependencies() -> tuple[Any, CandidatePOI, CandidateStay, tuple[WeatherRisk, ...]]:
    stay_case = next(
        item
        for item in load_stay_agent_suite().cases
        if item.case_id == "stay-beijing-history-low-walking-v1"
    )
    explore_case = next(
        item
        for item in load_explore_agent_suite().cases
        if item.case_id == "explore-beijing-history-v1"
    )
    poi = materialize_fixture_candidate(explore_case.provider_candidates[0])
    stay = materialize_stay_fixture_candidate(stay_case.provider_candidates[0])
    starts_at = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
    risks = (
        WeatherRisk(
            risk_id="weather-risk-rain-fixture",
            city="北京市",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=12),
            risk_type=WeatherRiskType.RAIN,
            severity=RiskSeverity.MEDIUM,
            threshold_description="fixture 预报包含中雨。",
            affected_activity_types=("outdoor",),
            advisory="主动提示减少长时间户外活动。",
            source=SourceReference(
                provider="weather-eval",
                provider_id="weather-beijing-fixture",
                data_mode=DataMode.FIXTURE,
                retrieved_at=datetime(2026, 8, 21, tzinfo=UTC),
                raw_response_sha256="1" * 64,
            ),
        ),
    )
    return stay_case.request, poi, stay, risks


def branch(result: SpecialistFanoutResult, name: SpecialistName) -> Any:
    return next(item for item in result.branches if item.specialist == name)


def make_settings(*, tracing: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("deepseek-test-secret-value"),
        langsmith_api_key=SecretStr("langsmith-test-secret-value"),
        langsmith_tracing=tracing,
    )


def test_three_specialists_enter_provider_concurrently_and_merge_without_overwrite() -> None:
    request, poi, stay, risks = sample_dependencies()
    provider = ConcurrentSpecialistProvider(
        poi,
        stay,
        risks,
        require_parallel_entry=True,
    )

    result = asyncio.run(
        run_specialist_fanout(
            request,
            provider,
            FixedExploreModel(),
            FixedStayModel(),
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == SpecialistFanoutStatus.COMPLETE
    assert tuple(item.specialist for item in result.branches) == tuple(SpecialistName)
    assert all(item.status == SpecialistBranchStatus.SUCCEEDED for item in result.branches)
    assert provider.peak_active == 3
    assert result.total_model_call_count == 4
    assert result.total_provider_call_count == 3
    assert branch(result, SpecialistName.EXPLORE).explore_result is not None
    assert branch(result, SpecialistName.STAY).stay_result is not None
    assert branch(result, SpecialistName.WEATHER).weather_risks == risks
    assert result.fanout_latency_ms < sum(item.elapsed_ms for item in result.branches)


def test_five_explore_queries_fit_the_fanout_call_contract() -> None:
    request, poi, stay, risks = sample_dependencies()
    provider = ConcurrentSpecialistProvider(poi, stay, risks)

    result = asyncio.run(
        run_specialist_fanout(
            request,
            provider,
            FiveQueryExploreModel(),
            FixedStayModel(),
            data_mode=DataMode.FIXTURE,
        )
    )

    explore = branch(result, SpecialistName.EXPLORE)
    assert result.status == SpecialistFanoutStatus.COMPLETE
    assert explore.status == SpecialistBranchStatus.SUCCEEDED
    assert explore.provider_call_count == 5
    assert explore.explore_result is not None
    assert len(explore.explore_result.queries) == 5
    assert provider.poi_calls == 5
    assert result.total_provider_call_count == 7


@pytest.mark.parametrize("failed_specialist", [SpecialistName.EXPLORE, SpecialistName.WEATHER])
def test_one_dependency_failure_preserves_other_specialist_results(
    failed_specialist: SpecialistName,
) -> None:
    request, poi, stay, risks = sample_dependencies()
    provider = ConcurrentSpecialistProvider(poi, stay, risks, failure=failed_specialist)

    result = asyncio.run(
        run_specialist_fanout(
            request,
            provider,
            FixedExploreModel(),
            FixedStayModel(),
            data_mode=DataMode.FIXTURE,
        )
    )

    failed = branch(result, failed_specialist)
    assert result.status == SpecialistFanoutStatus.PARTIAL
    assert failed.status == SpecialistBranchStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.category == SpecialistFailureCategory.PROVIDER
    assert failed.failure.provider_category == ProviderErrorCategory.TIMEOUT
    assert failed.failure.retryable is True
    assert sum(item.status == SpecialistBranchStatus.SUCCEEDED for item in result.branches) == 2
    assert branch(result, SpecialistName.STAY).stay_result is not None


def test_failed_branch_preserves_completed_model_usage_for_cost_accounting() -> None:
    request, poi, stay, risks = sample_dependencies()

    result = asyncio.run(
        run_specialist_fanout(
            request,
            ConcurrentSpecialistProvider(
                poi,
                stay,
                risks,
                failure=SpecialistName.EXPLORE,
            ),
            UsageExploreModel(),
            FixedStayModel(),
            data_mode=DataMode.FIXTURE,
        )
    )

    explore = branch(result, SpecialistName.EXPLORE)
    assert explore.status == SpecialistBranchStatus.FAILED
    assert explore.model_call_count == 1
    assert explore.model_usages == (
        ModelTokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )


def test_missing_rooms_skips_stay_without_stopping_explore_or_weather() -> None:
    _, poi, stay, risks = sample_dependencies()
    request = next(
        item
        for item in load_stay_agent_suite().cases
        if item.case_id == "stay-blocked-missing-rooms-v1"
    ).request
    provider = ConcurrentSpecialistProvider(poi, stay, risks)
    explore_model = FixedExploreModel()
    stay_model = FixedStayModel()

    result = asyncio.run(
        run_specialist_fanout(
            request,
            provider,
            explore_model,
            stay_model,
            data_mode=DataMode.FIXTURE,
        )
    )

    stay_branch = branch(result, SpecialistName.STAY)
    assert result.status == SpecialistFanoutStatus.PARTIAL
    assert stay_branch.status == SpecialistBranchStatus.SKIPPED
    assert stay_branch.skip_reason == SpecialistSkipReason.CAPABILITY_BLOCKED
    assert stay_branch.model_call_count == stay_branch.provider_call_count == 0
    assert stay_model.query_calls == stay_model.selection_calls == 0
    assert provider.stay_calls == 0
    assert branch(result, SpecialistName.EXPLORE).status == SpecialistBranchStatus.SUCCEEDED
    assert branch(result, SpecialistName.WEATHER).status == SpecialistBranchStatus.SUCCEEDED


def test_unsupported_destination_blocks_all_specialists_before_dependencies() -> None:
    _, poi, stay, risks = sample_dependencies()
    request = next(
        item
        for item in load_stay_agent_suite().cases
        if item.case_id == "stay-blocked-unsupported-nanjing-v1"
    ).request
    provider = ConcurrentSpecialistProvider(poi, stay, risks)
    explore_model = FixedExploreModel()
    stay_model = FixedStayModel()

    result = asyncio.run(
        run_specialist_fanout(
            request,
            provider,
            explore_model,
            stay_model,
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == SpecialistFanoutStatus.BLOCKED
    assert all(item.status == SpecialistBranchStatus.SKIPPED for item in result.branches)
    assert result.total_model_call_count == result.total_provider_call_count == 0
    assert explore_model.query_calls == explore_model.selection_calls == 0
    assert stay_model.query_calls == stay_model.selection_calls == 0
    assert provider.poi_calls == provider.stay_calls == provider.weather_calls == 0


def test_result_contract_rejects_duplicate_branch_overwrite() -> None:
    request, poi, stay, risks = sample_dependencies()
    result = asyncio.run(
        run_specialist_fanout(
            request,
            ConcurrentSpecialistProvider(poi, stay, risks),
            FixedExploreModel(),
            FixedStayModel(),
            data_mode=DataMode.FIXTURE,
        )
    )
    payload = copy.deepcopy(result.model_dump(mode="json"))
    payload["branches"][1] = payload["branches"][0]

    with pytest.raises(ValidationError, match="one ordered result"):
        SpecialistFanoutResult.model_validate(payload)


def test_sqlite_snapshot_restores_completed_fanout_without_dependency_replay(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        request, poi, stay, risks = sample_dependencies()
        checkpoint_path = tmp_path / "specialist-fanout.sqlite"
        async with open_sqlite_specialist_runtime(
            checkpoint_path,
            ConcurrentSpecialistProvider(poi, stay, risks),
            FixedExploreModel(),
            FixedStayModel(),
        ) as runtime:
            completed = await runtime.start(
                "specialist-thread-v1",
                request,
                data_mode=DataMode.FIXTURE,
            )
            assert completed.next_nodes == ()
            assert completed.result.status == SpecialistFanoutStatus.COMPLETE

        async with open_sqlite_specialist_runtime(
            checkpoint_path,
            FailIfCalledProvider(poi, stay, risks),
            FailIfCalledExploreModel(),
            FailIfCalledStayModel(),
        ) as restored_runtime:
            restored = await restored_runtime.snapshot("specialist-thread-v1")

        assert restored.result == completed.result
        assert restored.request == completed.request
        assert restored.checkpoint_id == completed.checkpoint_id

    asyncio.run(exercise())


def test_sqlite_runtime_rejects_duplicate_thread(tmp_path: Path) -> None:
    async def exercise() -> None:
        request, poi, stay, risks = sample_dependencies()
        async with open_sqlite_specialist_runtime(
            tmp_path / "duplicate-specialist.sqlite",
            ConcurrentSpecialistProvider(poi, stay, risks),
            FixedExploreModel(),
            FixedStayModel(),
        ) as runtime:
            await runtime.start(
                "duplicate-specialist-thread",
                request,
                data_mode=DataMode.FIXTURE,
            )
            with pytest.raises(DuplicateSpecialistThreadError, match="already"):
                await runtime.start(
                    "duplicate-specialist-thread",
                    request,
                    data_mode=DataMode.FIXTURE,
                )

    asyncio.run(exercise())


def test_live_runner_requires_langsmith_tracing() -> None:
    request, poi, stay, risks = sample_dependencies()
    with pytest.raises(SpecialistFanoutConfigurationError, match="LANGSMITH_TRACING"):
        asyncio.run(
            run_live_specialist_fanout(
                request,
                ConcurrentSpecialistProvider(poi, stay, risks),
                make_settings(tracing=False),
                data_mode=DataMode.FIXTURE,
            )
        )


def test_live_runner_flushes_outer_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    request, poi, stay, risks = sample_dependencies()
    fake_langsmith = FakeLangSmithClient()
    monkeypatch.setattr(fanout_module, "build_langsmith_client", lambda *_: fake_langsmith)
    monkeypatch.setattr(
        fanout_module, "DeepSeekExploreProposalModel", lambda *_: FixedExploreModel()
    )
    monkeypatch.setattr(fanout_module, "DeepSeekStayProposalModel", lambda *_: FixedStayModel())
    monkeypatch.setattr(fanout_module, "tracing_context", lambda **_: nullcontext())

    result = asyncio.run(
        run_live_specialist_fanout(
            request,
            ConcurrentSpecialistProvider(poi, stay, risks),
            make_settings(),
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == SpecialistFanoutStatus.COMPLETE
    assert fake_langsmith.flush_timeouts == [15.0]
