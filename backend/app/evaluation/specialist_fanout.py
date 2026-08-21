import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

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
from app.domain.candidates import CandidatePOI, CandidateStay
from app.domain.provider import ProviderErrorCategory, ProviderFailure
from app.domain.request import TripRequest
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import RiskSeverity, WeatherRisk, WeatherRiskType
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.explore import (
    load_explore_agent_suite,
    materialize_fixture_candidate,
)
from app.evaluation.specialist_fanout_contracts import (
    SpecialistFanoutBaselineReport,
    SpecialistFanoutCaseResult,
    SpecialistFanoutEvalCase,
    SpecialistFanoutEvalSuite,
    nearest_rank,
)
from app.evaluation.stay import (
    load_stay_agent_suite,
    materialize_stay_fixture_candidate,
)
from app.planning.specialist_contracts import (
    SpecialistBranchResult,
    SpecialistBranchStatus,
    SpecialistFailureCategory,
    SpecialistFanoutResult,
    SpecialistFanoutStatus,
    SpecialistName,
)
from app.providers.errors import ProviderRequestError
from app.providers.ports import POISearchRequest, StaySearchRequest, WeatherRiskRequest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SPECIALIST_FANOUT_SUITE_PATH = (
    REPOSITORY_ROOT / "evals" / "cases" / "specialist-fanout" / "suite.v1.json"
)
WEATHER_PROMPT_TERMS = ("天气", "下雨", "降雨", "高温", "大风", "下雪")

SpecialistFanoutRunner = Callable[
    [TripRequest, "SpecialistScenarioProvider"],
    Awaitable[SpecialistFanoutResult],
]


class SpecialistFanoutEvaluationError(RuntimeError):
    """Raised when the specialist fan-out evaluation fixture is inconsistent."""


class FixtureExploreModel:
    def propose_queries(self, context: Any) -> ExploreQueryModelResponse:
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


class FixtureStayModel:
    def propose_queries(self, context: Any) -> StayQueryModelResponse:
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


class SpecialistScenarioProvider:
    """One traceable fixture catalog with optional typed timeout injection."""

    def __init__(
        self,
        poi_candidates: tuple[CandidatePOI, ...],
        stay_candidates: tuple[CandidateStay, ...],
        weather_risks: tuple[WeatherRisk, ...],
        *,
        failure: SpecialistName | None,
        require_parallel_entry: bool,
    ) -> None:
        self.poi_candidates = poi_candidates
        self.stay_candidates = stay_candidates
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
        await asyncio.wait_for(self._gate_open.wait(), timeout=30.0)
        await asyncio.sleep(0.03)
        self._active -= 1

    def _raise_if_failed(self, specialist: SpecialistName, operation: str) -> None:
        if self.failure != specialist:
            return
        raise ProviderRequestError(
            ProviderFailure(
                provider="specialist-fanout-eval",
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
        return self.poi_candidates[: request.limit]

    async def search_stays(self, request: StaySearchRequest) -> tuple[CandidateStay, ...]:
        self.stay_calls += 1
        await self._gate(SpecialistName.STAY)
        self._raise_if_failed(SpecialistName.STAY, "search_stays")
        return self.stay_candidates[: request.limit]

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        del request
        self.weather_calls += 1
        await self._gate(SpecialistName.WEATHER)
        self._raise_if_failed(SpecialistName.WEATHER, "get_weather_risks")
        return self.weather_risks


def load_specialist_fanout_suite(
    suite_path: Path = SPECIALIST_FANOUT_SUITE_PATH,
) -> SpecialistFanoutEvalSuite:
    return SpecialistFanoutEvalSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))


def _referenced_fixture_payload(
    suite: SpecialistFanoutEvalSuite,
) -> tuple[dict[str, object], ...]:
    explore_by_id = {item.case_id: item for item in load_explore_agent_suite().cases}
    stay_by_id = {item.case_id: item for item in load_stay_agent_suite().cases}
    payloads: list[dict[str, object]] = []
    for case in suite.cases:
        try:
            explore_case = explore_by_id[case.explore_fixture_case_id]
            stay_case = stay_by_id[case.stay_fixture_case_id]
        except KeyError as error:
            raise SpecialistFanoutEvaluationError(
                f"unknown referenced fixture case: {error.args[0]}"
            ) from error
        payloads.append(
            {
                "case_id": case.case_id,
                "explore_fixture": explore_case.model_dump(mode="json"),
                "stay_fixture": stay_case.model_dump(mode="json"),
            }
        )
    return tuple(payloads)


def specialist_fanout_dataset_sha256(suite: SpecialistFanoutEvalSuite) -> str:
    canonical = json.dumps(
        {
            "suite": suite.model_dump(mode="json"),
            "referenced_fixtures": _referenced_fixture_payload(suite),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _weather_risks_for_case(case: SpecialistFanoutEvalCase) -> tuple[WeatherRisk, ...]:
    starts_at = datetime.combine(case.request.start_date, time(8), tzinfo=UTC)
    material = f"{case.case_id}|rain|fixture"
    return (
        WeatherRisk(
            risk_id=f"weather-{case.case_id}",
            city=case.request.destination_city,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=12),
            risk_type=WeatherRiskType.RAIN,
            severity=RiskSeverity.MEDIUM,
            threshold_description="fixture 预报包含中雨。",
            affected_activity_types=("outdoor",),
            advisory="主动提示减少长时间户外活动。",
            source=SourceReference(
                provider="specialist-weather-eval",
                provider_id=f"weather-{case.case_id}",
                data_mode=DataMode.FIXTURE,
                retrieved_at=datetime(2026, 8, 21, tzinfo=UTC),
                raw_response_sha256=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            ),
        ),
    )


def build_specialist_scenario_provider(
    case: SpecialistFanoutEvalCase,
) -> SpecialistScenarioProvider:
    explore_by_id = {item.case_id: item for item in load_explore_agent_suite().cases}
    stay_by_id = {item.case_id: item for item in load_stay_agent_suite().cases}
    try:
        explore_case = explore_by_id[case.explore_fixture_case_id]
        stay_case = stay_by_id[case.stay_fixture_case_id]
    except KeyError as error:
        raise SpecialistFanoutEvaluationError(
            f"unknown referenced fixture case: {error.args[0]}"
        ) from error
    return SpecialistScenarioProvider(
        tuple(materialize_fixture_candidate(item) for item in explore_case.provider_candidates),
        tuple(materialize_stay_fixture_candidate(item) for item in stay_case.provider_candidates),
        _weather_risks_for_case(case),
        failure=case.injected_provider_failure,
        require_parallel_entry=case.expected.require_parallel_provider_entry,
    )


def _branch(result: SpecialistFanoutResult, specialist: SpecialistName) -> SpecialistBranchResult:
    return next(item for item in result.branches if item.specialist == specialist)


def _source_traceable(result: SpecialistFanoutResult) -> bool:
    sources: list[SourceReference] = []
    for branch in result.branches:
        if branch.explore_result is not None:
            sources.extend(item.candidate.source for item in branch.explore_result.recommendations)
        if branch.stay_result is not None:
            sources.extend(item.candidate.source for item in branch.stay_result.recommendations)
        sources.extend(item.source for item in branch.weather_risks)
    return all(
        source.provider
        and source.provider_id
        and source.data_mode == DataMode.FIXTURE
        and source.raw_response_sha256
        for source in sources
    )


def _usages(result: SpecialistFanoutResult) -> tuple[ModelTokenUsage, ...]:
    return tuple(usage for branch in result.branches for usage in branch.model_usages)


def _error_code(error: Exception) -> str:
    material = f"{error.__class__.__module__}|{error.__class__.__qualname__}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]
    return f"specialist-eval-error-{digest}"


def _failed_case_result(
    case: SpecialistFanoutEvalCase,
    provider: SpecialistScenarioProvider,
    error: Exception,
) -> SpecialistFanoutCaseResult:
    checks = (
        EvaluationCheck(code="workflow_completed", passed=False),
        EvaluationCheck(code="fanout_status_matches", passed=False),
        EvaluationCheck(code="branch_statuses_match", passed=False),
        EvaluationCheck(code="exact_ordered_merge", passed=False),
        EvaluationCheck(code="typed_provider_failure", passed=False),
        EvaluationCheck(code="successful_branches_preserved", passed=False),
        EvaluationCheck(code="proactive_weather_lookup", passed=False),
        EvaluationCheck(code="blocked_case_uses_zero_calls", passed=False),
        EvaluationCheck(code="parallel_provider_entry", passed=False),
        EvaluationCheck(code="source_traceability", passed=False),
    )
    return SpecialistFanoutCaseResult(
        case_id=case.case_id,
        expected_status=case.expected.status,
        passed=False,
        actual_branch_count=0,
        branch_status_match_count=0,
        exact_ordered_merge=False,
        typed_provider_failure_count=0,
        preserved_success_count=0,
        proactive_weather_call_count=provider.weather_calls,
        model_call_count=0,
        provider_call_count=provider.poi_calls + provider.stay_calls + provider.weather_calls,
        usage_call_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        parallel_provider_peak=provider.peak_active,
        source_traceability_passed=False,
        fanout_latency_ms=0,
        branch_latency_sum_ms=0,
        error_code=_error_code(error),
        checks=checks,
    )


async def evaluate_specialist_fanout_case(
    case: SpecialistFanoutEvalCase,
    runner: SpecialistFanoutRunner,
) -> SpecialistFanoutCaseResult:
    provider = build_specialist_scenario_provider(case)
    try:
        result = await runner(case.request, provider)
    except Exception as error:
        return _failed_case_result(case, provider, error)

    expected_by_name = {item.specialist: item for item in case.expected.branches}
    branch_matches = sum(
        branch.status == expected_by_name[branch.specialist].status for branch in result.branches
    )
    exact_merge = tuple(item.specialist for item in result.branches) == tuple(SpecialistName)
    failed_branches = tuple(
        item for item in result.branches if item.status == SpecialistBranchStatus.FAILED
    )
    typed_failures = tuple(
        item
        for item in failed_branches
        if item.failure is not None
        and item.failure.category == SpecialistFailureCategory.PROVIDER
        and item.failure.provider_category
        == expected_by_name[item.specialist].provider_failure_category
        and item.failure.retryable
    )
    preserved_success_count = (
        sum(item.status == SpecialistBranchStatus.SUCCEEDED for item in result.branches)
        if case.injected_provider_failure is not None
        else 0
    )
    weather_expected = expected_by_name[SpecialistName.WEATHER]
    expected_weather_calls = 0 if weather_expected.status == SpecialistBranchStatus.SKIPPED else 1
    weather_prompt_absent = not any(term in case.request.raw_text for term in WEATHER_PROMPT_TERMS)
    proactive_weather_calls = (
        provider.weather_calls if weather_prompt_absent and expected_weather_calls else 0
    )
    call_count_matches = result.total_provider_call_count == (
        provider.poi_calls + provider.stay_calls + provider.weather_calls
    )
    blocked_zero_calls = (
        result.total_model_call_count == 0 and result.total_provider_call_count == 0
        if case.expected.status == SpecialistFanoutStatus.BLOCKED
        else True
    )
    parallel_evidence = (
        provider.peak_active == 3
        and result.fanout_latency_ms < sum(item.elapsed_ms for item in result.branches)
        if case.expected.require_parallel_provider_entry
        else True
    )
    source_traceability = _source_traceable(result)
    usages = _usages(result)
    checks = (
        EvaluationCheck(code="workflow_completed", passed=True),
        EvaluationCheck(
            code="fanout_status_matches",
            passed=result.status == case.expected.status,
        ),
        EvaluationCheck(code="branch_statuses_match", passed=branch_matches == 3),
        EvaluationCheck(
            code="exact_ordered_merge",
            passed=exact_merge and len(result.branches) == 3,
        ),
        EvaluationCheck(
            code="typed_provider_failure",
            passed=len(typed_failures) == len(failed_branches),
        ),
        EvaluationCheck(
            code="successful_branches_preserved",
            passed=(
                preserved_success_count == 2 if case.injected_provider_failure is not None else True
            ),
        ),
        EvaluationCheck(
            code="proactive_weather_lookup",
            passed=(
                weather_prompt_absent
                and provider.weather_calls == expected_weather_calls
                and _branch(result, SpecialistName.WEATHER).model_call_count == 0
            ),
        ),
        EvaluationCheck(code="blocked_case_uses_zero_calls", passed=blocked_zero_calls),
        EvaluationCheck(code="parallel_provider_entry", passed=parallel_evidence),
        EvaluationCheck(code="source_traceability", passed=source_traceability),
        EvaluationCheck(code="call_aggregates_match", passed=call_count_matches),
    )
    return SpecialistFanoutCaseResult(
        case_id=case.case_id,
        expected_status=case.expected.status,
        actual_status=result.status,
        passed=all(check.passed for check in checks),
        actual_branch_count=len(result.branches),
        branch_status_match_count=branch_matches,
        exact_ordered_merge=exact_merge,
        typed_provider_failure_count=len(typed_failures),
        preserved_success_count=preserved_success_count,
        proactive_weather_call_count=proactive_weather_calls,
        model_call_count=result.total_model_call_count,
        provider_call_count=result.total_provider_call_count,
        usage_call_count=len(usages),
        prompt_tokens=sum(item.prompt_tokens for item in usages),
        completion_tokens=sum(item.completion_tokens for item in usages),
        total_tokens=sum(item.total_tokens for item in usages),
        parallel_provider_peak=provider.peak_active,
        source_traceability_passed=source_traceability,
        fanout_latency_ms=result.fanout_latency_ms,
        branch_latency_sum_ms=sum(item.elapsed_ms for item in result.branches),
        checks=checks,
    )


async def evaluate_specialist_fanout_suite(
    runner: SpecialistFanoutRunner,
    *,
    execution_mode: Literal["fixture", "live"],
    explore_model: str,
    stay_model: str,
    suite_path: Path = SPECIALIST_FANOUT_SUITE_PATH,
) -> SpecialistFanoutBaselineReport:
    suite = load_specialist_fanout_suite(suite_path)
    results = tuple([await evaluate_specialist_fanout_case(case, runner) for case in suite.cases])
    passed_count = sum(item.passed for item in results)
    branch_matches = sum(item.branch_status_match_count for item in results)
    latencies = sorted(item.fanout_latency_ms for item in results)
    return SpecialistFanoutBaselineReport(
        execution_mode=execution_mode,
        explore_model=explore_model,
        stay_model=stay_model,
        dataset_sha256=specialist_fanout_dataset_sha256(suite),
        passed_case_count=passed_count,
        case_pass_rate=expected_rate(passed_count, len(results)),
        branch_status_match_count=branch_matches,
        branch_status_accuracy=expected_rate(branch_matches, 15),
        exact_ordered_merge_case_count=sum(item.exact_ordered_merge for item in results),
        typed_provider_failure_count=sum(item.typed_provider_failure_count for item in results),
        preserved_success_count=sum(item.preserved_success_count for item in results),
        proactive_weather_call_count=sum(item.proactive_weather_call_count for item in results),
        blocked_zero_call_case_count=sum(
            item.actual_status == SpecialistFanoutStatus.BLOCKED
            and item.model_call_count == 0
            and item.provider_call_count == 0
            for item in results
        ),
        parallel_provider_entry_case_count=sum(
            item.parallel_provider_peak == 3 for item in results
        ),
        source_traceability_case_count=sum(
            item.actual_status != SpecialistFanoutStatus.BLOCKED and item.source_traceability_passed
            for item in results
        ),
        model_call_count=sum(item.model_call_count for item in results),
        provider_call_count=sum(item.provider_call_count for item in results),
        usage_call_count=sum(item.usage_call_count for item in results),
        total_prompt_tokens=sum(item.prompt_tokens for item in results),
        total_completion_tokens=sum(item.completion_tokens for item in results),
        total_tokens=sum(item.total_tokens for item in results),
        p50_fanout_latency_ms=nearest_rank(latencies, 50),
        p95_fanout_latency_ms=nearest_rank(latencies, 95),
        results=results,
        limitations=(
            "五条案例使用显式 fixture 候选与天气风险, 不评估实时高德召回率或天气新鲜度。",
            "两条超时是可控的 Provider 故障注入, 证明降级契约而非生产故障率。",
            "并发证据证明三个 Provider 分支同时在途, 不代表所有外部服务都有相同延迟。",
            "当前输出是专业信息包而非最终 TripPlan, 尚不能声称多 Agent 提升最终行程质量。",
            "该开发集用于编排回归, 不应解释为未见数据上的泛化准确率。",
        ),
    )
