import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.agents.contracts import ExploreAgentResult
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerContext
from app.domain.sources import DataMode, SourceReference
from app.domain.travel_data import RouteLeg, WeatherRisk
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.explore_contracts import (
    ExploreAgentBaselineReport,
    ExploreAgentCaseResult,
    ExploreAgentEvalCase,
    ExploreAgentEvalSuite,
    ExploreFixtureCandidateSpec,
    _nearest_rank,
)
from app.planning import compile_planner_context
from app.providers.ports import POISearchRequest, RouteRequest, WeatherRiskRequest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXPLORE_AGENT_SUITE_PATH = REPOSITORY_ROOT / "evals" / "cases" / "explore-agent" / "suite.v1.json"

ExploreRunner = Callable[
    [PlannerContext, "ExploreScenarioProvider"],
    Awaitable[ExploreAgentResult],
]


class ExploreEvaluationError(RuntimeError):
    """Raised when an Explore evaluation fixture violates its frozen contract."""


def _candidate_source_hash(spec: ExploreFixtureCandidateSpec) -> str:
    canonical = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def materialize_fixture_candidate(spec: ExploreFixtureCandidateSpec) -> CandidatePOI:
    return CandidatePOI(
        candidate_id=spec.candidate_id,
        name=spec.name,
        city=spec.city,
        district=spec.district,
        address=spec.address,
        location=spec.location,
        categories=spec.categories,
        environment=spec.environment,
        tags=spec.tags,
        source=SourceReference(
            provider="explore-eval-catalog",
            provider_id=spec.provider_id,
            data_mode=DataMode.FIXTURE,
            retrieved_at=datetime(2026, 8, 21, tzinfo=UTC),
            raw_response_sha256=_candidate_source_hash(spec),
        ),
    )


class ExploreScenarioProvider:
    """Return one explicit three-item fixture catalog for every case query."""

    def __init__(self, specs: tuple[ExploreFixtureCandidateSpec, ...]) -> None:
        self.candidates = tuple(materialize_fixture_candidate(item) for item in specs)
        self.calls: list[POISearchRequest] = []

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        self.calls.append(request)
        return self.candidates[: request.limit]

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        raise ExploreEvaluationError(f"unexpected weather call for {request.city_adcode}")

    async def get_route(self, request: RouteRequest) -> RouteLeg:
        raise ExploreEvaluationError(f"unexpected route call for {request.city_adcode}")


def load_explore_agent_suite(
    suite_path: Path = EXPLORE_AGENT_SUITE_PATH,
) -> ExploreAgentEvalSuite:
    return ExploreAgentEvalSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))


def explore_agent_dataset_sha256(suite: ExploreAgentEvalSuite) -> str:
    canonical = json.dumps(
        suite.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _traceable(candidate: CandidatePOI) -> bool:
    return bool(
        candidate.source.provider
        and candidate.source.provider_id
        and candidate.source.data_mode == DataMode.FIXTURE
        and candidate.source.raw_response_sha256
    )


def _error_code(error: Exception) -> str:
    name = error.__class__.__name__.casefold()
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"explore-eval-error-{digest}"


async def evaluate_explore_agent_case(
    case: ExploreAgentEvalCase,
    runner: ExploreRunner,
) -> ExploreAgentCaseResult:
    context = compile_planner_context(case.request)
    provider = ExploreScenarioProvider(case.provider_candidates)
    expected = case.expected
    checks: tuple[EvaluationCheck, ...]
    try:
        result = await runner(context, provider)
    except Exception as error:
        checks = (
            EvaluationCheck(code="explore_protocol_succeeded", passed=False),
            EvaluationCheck(code="query_kind_coverage", passed=False),
            EvaluationCheck(code="context_reference_coverage", passed=False),
            EvaluationCheck(code="provider_calls_match_queries", passed=False),
            EvaluationCheck(code="candidate_grounding", passed=False),
            EvaluationCheck(code="source_traceability", passed=False),
            EvaluationCheck(code="labelled_relevance", passed=False),
            EvaluationCheck(code="recommendation_group_coverage", passed=False),
        )
        return ExploreAgentCaseResult(
            case_id=case.case_id,
            passed=False,
            model_call_count=0,
            provider_call_count=len(provider.calls),
            query_count=0,
            required_query_kind_count=len(expected.required_query_kinds),
            matched_query_kind_count=0,
            recommendation_count=0,
            grounded_recommendation_count=0,
            traceable_recommendation_count=0,
            allowed_recommendation_count=0,
            required_recommendation_group_count=len(expected.required_recommendation_groups),
            matched_recommendation_group_count=0,
            query_latency_ms=0,
            selection_latency_ms=0,
            error_code=_error_code(error),
            checks=checks,
        )

    required_kinds = set(expected.required_query_kinds)
    actual_kinds = {item.kind for item in result.queries}
    matched_kinds = required_kinds & actual_kinds
    actual_context_refs = {ref for item in result.queries for ref in item.context_refs}
    observed_by_id = {item.candidate.candidate_id: item.candidate for item in result.observations}
    grounded = tuple(
        item
        for item in result.recommendations
        if item.candidate.candidate_id in observed_by_id
        and item.candidate == observed_by_id[item.candidate.candidate_id]
    )
    traceable = tuple(item for item in grounded if _traceable(item.candidate))
    recommendation_ids = {item.candidate.candidate_id for item in result.recommendations}
    allowed_ids = set(expected.allowed_recommendation_ids)
    allowed_count = sum(item in allowed_ids for item in recommendation_ids)
    matched_groups = sum(
        bool(recommendation_ids & set(group)) for group in expected.required_recommendation_groups
    )
    checks = (
        EvaluationCheck(code="explore_protocol_succeeded", passed=True),
        EvaluationCheck(
            code="request_and_context_match",
            passed=(
                result.request_id == context.request_id and result.context_id == context.context_id
            ),
        ),
        EvaluationCheck(
            code="query_kind_coverage",
            passed=matched_kinds == required_kinds,
        ),
        EvaluationCheck(
            code="context_reference_coverage",
            passed=set(expected.required_context_refs).issubset(actual_context_refs),
        ),
        EvaluationCheck(
            code="provider_calls_match_queries",
            passed=len(provider.calls) == len(result.queries),
        ),
        EvaluationCheck(
            code="candidate_grounding",
            passed=len(grounded) == len(result.recommendations),
        ),
        EvaluationCheck(
            code="source_traceability",
            passed=len(traceable) == len(grounded),
        ),
        EvaluationCheck(
            code="labelled_relevance",
            passed=(
                recommendation_ids.issubset(allowed_ids)
                and not recommendation_ids.intersection(expected.forbidden_recommendation_ids)
            ),
        ),
        EvaluationCheck(
            code="recommendation_group_coverage",
            passed=matched_groups == len(expected.required_recommendation_groups),
        ),
    )
    return ExploreAgentCaseResult(
        case_id=case.case_id,
        passed=all(check.passed for check in checks),
        model_call_count=2,
        provider_call_count=len(provider.calls),
        query_count=len(result.queries),
        required_query_kind_count=len(required_kinds),
        matched_query_kind_count=len(matched_kinds),
        recommendation_count=len(result.recommendations),
        grounded_recommendation_count=len(grounded),
        traceable_recommendation_count=len(traceable),
        allowed_recommendation_count=allowed_count,
        required_recommendation_group_count=len(expected.required_recommendation_groups),
        matched_recommendation_group_count=matched_groups,
        query_latency_ms=result.query_latency_ms,
        selection_latency_ms=result.selection_latency_ms,
        query_usage=result.query_usage,
        selection_usage=result.selection_usage,
        checks=checks,
    )


async def evaluate_explore_agent_suite(
    runner: ExploreRunner,
    *,
    execution_mode: Literal["fixture", "live"],
    model: str,
    suite_path: Path = EXPLORE_AGENT_SUITE_PATH,
) -> ExploreAgentBaselineReport:
    if execution_mode not in {"fixture", "live"}:
        raise ExploreEvaluationError("execution_mode must be fixture or live")
    suite = load_explore_agent_suite(suite_path)
    results = tuple([await evaluate_explore_agent_case(case, runner) for case in suite.cases])
    passed_count = sum(item.passed for item in results)
    required_kind_count = sum(item.required_query_kind_count for item in results)
    matched_kind_count = sum(item.matched_query_kind_count for item in results)
    recommendation_count = sum(item.recommendation_count for item in results)
    grounded_count = sum(item.grounded_recommendation_count for item in results)
    traceable_count = sum(item.traceable_recommendation_count for item in results)
    allowed_count = sum(item.allowed_recommendation_count for item in results)
    required_group_count = sum(item.required_recommendation_group_count for item in results)
    matched_group_count = sum(item.matched_recommendation_group_count for item in results)
    usages = tuple(
        usage
        for item in results
        for usage in (item.query_usage, item.selection_usage)
        if usage is not None
    )
    case_latencies = sorted(
        item.query_latency_ms + item.selection_latency_ms
        for item in results
        if item.model_call_count > 0
    )
    return ExploreAgentBaselineReport(
        execution_mode=execution_mode,
        model=model,
        dataset_sha256=explore_agent_dataset_sha256(suite),
        passed_case_count=passed_count,
        case_pass_rate=expected_rate(passed_count, len(results)),
        model_call_count=sum(item.model_call_count for item in results),
        provider_call_count=sum(item.provider_call_count for item in results),
        required_query_kind_count=required_kind_count,
        matched_query_kind_count=matched_kind_count,
        query_kind_coverage_rate=expected_rate(matched_kind_count, required_kind_count),
        recommendation_count=recommendation_count,
        grounded_recommendation_count=grounded_count,
        grounding_rate=expected_rate(grounded_count, recommendation_count),
        traceable_recommendation_count=traceable_count,
        source_traceability_rate=expected_rate(traceable_count, grounded_count),
        allowed_recommendation_count=allowed_count,
        labelled_relevance_rate=expected_rate(allowed_count, recommendation_count),
        required_recommendation_group_count=required_group_count,
        matched_recommendation_group_count=matched_group_count,
        recommendation_group_coverage_rate=expected_rate(
            matched_group_count,
            required_group_count,
        ),
        usage_call_count=len(usages),
        total_prompt_tokens=sum(item.prompt_tokens for item in usages),
        total_completion_tokens=sum(item.completion_tokens for item in usages),
        total_tokens=sum(item.total_tokens for item in usages),
        p50_case_latency_ms=_nearest_rank(case_latencies, 50),
        p95_case_latency_ms=_nearest_rank(case_latencies, 95),
        results=results,
        limitations=(
            "六条案例使用显式 fixture 候选目录, 不评估实时高德搜索覆盖率或数据新鲜度。",
            "每条查询返回同一案例的三项目录以隔离 Agent 决策, 不把关键词召回率计入结果。",
            "人工标签只判断候选与给定偏好的相关性, 不代表景点质量、热度或个性化满意度。",
            "本报告不评估酒店、天气、路线、营业时间、票价、预算可行性或完整行程。",
        ),
    )
