import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.agents.contracts import StayAgentResult
from app.domain.candidates import CandidateStay
from app.domain.context import PlannerCapability, PlannerContext
from app.domain.sources import DataMode, SourceReference
from app.evaluation.contracts import EvaluationCheck, expected_rate
from app.evaluation.stay_contracts import (
    StayAgentBaselineReport,
    StayAgentCaseResult,
    StayAgentEvalCase,
    StayAgentEvalSuite,
    StayFixtureCandidateSpec,
    nearest_rank,
)
from app.planning import compile_planner_context
from app.providers.ports import StaySearchRequest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
STAY_AGENT_SUITE_PATH = REPOSITORY_ROOT / "evals" / "cases" / "stay-agent" / "suite.v1.json"

StayRunner = Callable[
    [PlannerContext, "StayScenarioProvider"],
    Awaitable[StayAgentResult],
]


class StayEvaluationError(RuntimeError):
    """Raised when a Stay evaluation fixture violates its frozen contract."""


def _candidate_source_hash(spec: StayFixtureCandidateSpec) -> str:
    canonical = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def materialize_stay_fixture_candidate(spec: StayFixtureCandidateSpec) -> CandidateStay:
    return CandidateStay(
        candidate_id=spec.candidate_id,
        name=spec.name,
        city=spec.city,
        district=spec.district,
        address=spec.address,
        location=spec.location,
        area_name=spec.area_name,
        tags=spec.tags,
        source=SourceReference(
            provider="stay-eval-catalog",
            provider_id=spec.provider_id,
            data_mode=DataMode.FIXTURE,
            retrieved_at=datetime(2026, 8, 21, tzinfo=UTC),
            raw_response_sha256=_candidate_source_hash(spec),
        ),
    )


class StayScenarioProvider:
    """Return one explicit three-item fixture catalog for every eligible case query."""

    def __init__(self, specs: tuple[StayFixtureCandidateSpec, ...]) -> None:
        self.candidates = tuple(materialize_stay_fixture_candidate(item) for item in specs)
        self.calls: list[StaySearchRequest] = []

    async def search_stays(self, request: StaySearchRequest) -> tuple[CandidateStay, ...]:
        self.calls.append(request)
        return self.candidates[: request.limit]


def load_stay_agent_suite(
    suite_path: Path = STAY_AGENT_SUITE_PATH,
) -> StayAgentEvalSuite:
    return StayAgentEvalSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))


def stay_agent_dataset_sha256(suite: StayAgentEvalSuite) -> str:
    canonical = json.dumps(
        suite.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _traceable(candidate: CandidateStay) -> bool:
    return bool(
        candidate.source.provider
        and candidate.source.provider_id
        and candidate.source.data_mode == DataMode.FIXTURE
        and candidate.source.raw_response_sha256
    )


def _error_code(error: Exception) -> str:
    name = error.__class__.__name__.casefold()
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"stay-eval-error-{digest}"


def _blocked_case_result(
    case: StayAgentEvalCase,
    context: PlannerContext,
) -> StayAgentCaseResult:
    expected_kind = case.expected.expected_clarification_kind
    actual_kinds = {item.kind for item in context.clarifications if item.blocking}
    checks = (
        EvaluationCheck(
            code="stay_capability_blocked",
            passed=PlannerCapability.STAY_SEARCH in context.blocked_capabilities,
        ),
        EvaluationCheck(
            code="expected_clarification_present",
            passed=expected_kind is not None and expected_kind in actual_kinds,
        ),
        EvaluationCheck(code="model_call_skipped", passed=True),
        EvaluationCheck(code="provider_call_skipped", passed=True),
        EvaluationCheck(code="no_fabricated_recommendations", passed=True),
    )
    return StayAgentCaseResult(
        case_id=case.case_id,
        expected_outcome="blocked",
        passed=all(check.passed for check in checks),
        model_call_count=0,
        provider_call_count=0,
        query_count=0,
        required_context_ref_count=0,
        matched_context_ref_count=0,
        recommendation_count=0,
        grounded_recommendation_count=0,
        traceable_recommendation_count=0,
        allowed_recommendation_count=0,
        required_recommendation_group_count=0,
        matched_recommendation_group_count=0,
        unverified_price_field_count=0,
        unknown_availability_count=0,
        booking_disabled_count=0,
        query_latency_ms=0,
        selection_latency_ms=0,
        checks=checks,
    )


def _failed_recommendation_case_result(
    case: StayAgentEvalCase,
    provider: StayScenarioProvider,
    error: Exception,
) -> StayAgentCaseResult:
    expected = case.expected
    checks = (
        EvaluationCheck(code="stay_protocol_succeeded", passed=False),
        EvaluationCheck(code="stay_capability_ready", passed=False),
        EvaluationCheck(code="context_reference_coverage", passed=False),
        EvaluationCheck(code="provider_calls_match_queries", passed=False),
        EvaluationCheck(code="candidate_grounding", passed=False),
        EvaluationCheck(code="source_traceability", passed=False),
        EvaluationCheck(code="labelled_relevance", passed=False),
        EvaluationCheck(code="recommendation_group_coverage", passed=False),
        EvaluationCheck(code="no_unverified_price_fields", passed=False),
        EvaluationCheck(code="availability_remains_unknown", passed=False),
        EvaluationCheck(code="booking_remains_disabled", passed=False),
    )
    return StayAgentCaseResult(
        case_id=case.case_id,
        expected_outcome="recommendations",
        passed=False,
        model_call_count=0,
        provider_call_count=len(provider.calls),
        query_count=0,
        required_context_ref_count=len(expected.required_context_refs),
        matched_context_ref_count=0,
        recommendation_count=0,
        grounded_recommendation_count=0,
        traceable_recommendation_count=0,
        allowed_recommendation_count=0,
        required_recommendation_group_count=len(expected.required_recommendation_groups),
        matched_recommendation_group_count=0,
        unverified_price_field_count=0,
        unknown_availability_count=0,
        booking_disabled_count=0,
        query_latency_ms=0,
        selection_latency_ms=0,
        error_code=_error_code(error),
        checks=checks,
    )


async def evaluate_stay_agent_case(
    case: StayAgentEvalCase,
    runner: StayRunner,
) -> StayAgentCaseResult:
    context = compile_planner_context(case.request)
    if case.expected.outcome == "blocked":
        return _blocked_case_result(case, context)
    provider = StayScenarioProvider(case.provider_candidates)
    expected = case.expected
    try:
        result = await runner(context, provider)
    except Exception as error:
        return _failed_recommendation_case_result(case, provider, error)

    required_refs = set(expected.required_context_refs)
    actual_refs = {ref for item in result.queries for ref in item.context_refs}
    matched_refs = required_refs & actual_refs
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
    unverified_price_fields = sum(
        any(
            value is not None
            for value in (
                item.candidate.nightly_price_estimate,
                item.candidate.price_basis,
                item.candidate.price_source,
            )
        )
        for item in result.recommendations
    )
    unknown_availability = sum(
        item.candidate.availability_status == "unknown" for item in result.recommendations
    )
    booking_disabled = sum(
        item.candidate.booking_supported is False for item in result.recommendations
    )
    checks = (
        EvaluationCheck(code="stay_protocol_succeeded", passed=True),
        EvaluationCheck(
            code="stay_capability_ready",
            passed=PlannerCapability.STAY_SEARCH in context.ready_capabilities,
        ),
        EvaluationCheck(
            code="request_and_context_match",
            passed=(
                result.request_id == context.request_id and result.context_id == context.context_id
            ),
        ),
        EvaluationCheck(
            code="context_reference_coverage",
            passed=matched_refs == required_refs,
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
        EvaluationCheck(
            code="no_unverified_price_fields",
            passed=unverified_price_fields == 0,
        ),
        EvaluationCheck(
            code="availability_remains_unknown",
            passed=unknown_availability == len(result.recommendations),
        ),
        EvaluationCheck(
            code="booking_remains_disabled",
            passed=booking_disabled == len(result.recommendations),
        ),
    )
    return StayAgentCaseResult(
        case_id=case.case_id,
        expected_outcome="recommendations",
        passed=all(check.passed for check in checks),
        model_call_count=2,
        provider_call_count=len(provider.calls),
        query_count=len(result.queries),
        required_context_ref_count=len(required_refs),
        matched_context_ref_count=len(matched_refs),
        recommendation_count=len(result.recommendations),
        grounded_recommendation_count=len(grounded),
        traceable_recommendation_count=len(traceable),
        allowed_recommendation_count=allowed_count,
        required_recommendation_group_count=len(expected.required_recommendation_groups),
        matched_recommendation_group_count=matched_groups,
        unverified_price_field_count=unverified_price_fields,
        unknown_availability_count=unknown_availability,
        booking_disabled_count=booking_disabled,
        query_latency_ms=result.query_latency_ms,
        selection_latency_ms=result.selection_latency_ms,
        query_usage=result.query_usage,
        selection_usage=result.selection_usage,
        checks=checks,
    )


async def evaluate_stay_agent_suite(
    runner: StayRunner,
    *,
    execution_mode: Literal["fixture", "live"],
    model: str,
    suite_path: Path = STAY_AGENT_SUITE_PATH,
) -> StayAgentBaselineReport:
    if execution_mode not in {"fixture", "live"}:
        raise StayEvaluationError("execution_mode must be fixture or live")
    suite = load_stay_agent_suite(suite_path)
    results = tuple([await evaluate_stay_agent_case(case, runner) for case in suite.cases])
    passed_count = sum(item.passed for item in results)
    required_ref_count = sum(item.required_context_ref_count for item in results)
    matched_ref_count = sum(item.matched_context_ref_count for item in results)
    recommendation_count = sum(item.recommendation_count for item in results)
    grounded_count = sum(item.grounded_recommendation_count for item in results)
    traceable_count = sum(item.traceable_recommendation_count for item in results)
    allowed_count = sum(item.allowed_recommendation_count for item in results)
    required_group_count = sum(item.required_recommendation_group_count for item in results)
    matched_group_count = sum(item.matched_recommendation_group_count for item in results)
    unverified_price_fields = sum(item.unverified_price_field_count for item in results)
    unknown_availability = sum(item.unknown_availability_count for item in results)
    booking_disabled = sum(item.booking_disabled_count for item in results)
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
    return StayAgentBaselineReport(
        execution_mode=execution_mode,
        model=model,
        dataset_sha256=stay_agent_dataset_sha256(suite),
        passed_case_count=passed_count,
        case_pass_rate=expected_rate(passed_count, len(results)),
        model_call_count=sum(item.model_call_count for item in results),
        provider_call_count=sum(item.provider_call_count for item in results),
        required_context_ref_count=required_ref_count,
        matched_context_ref_count=matched_ref_count,
        context_reference_coverage_rate=expected_rate(matched_ref_count, required_ref_count),
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
        unverified_price_field_count=unverified_price_fields,
        unknown_availability_count=unknown_availability,
        booking_disabled_count=booking_disabled,
        commercial_truth_boundary_passed=(
            unverified_price_fields == 0
            and unknown_availability == recommendation_count
            and booking_disabled == recommendation_count
        ),
        usage_call_count=len(usages),
        total_prompt_tokens=sum(item.prompt_tokens for item in usages),
        total_completion_tokens=sum(item.completion_tokens for item in usages),
        total_tokens=sum(item.total_tokens for item in usages),
        p50_case_latency_ms=nearest_rank(case_latencies, 50),
        p95_case_latency_ms=nearest_rank(case_latencies, 95),
        results=results,
        limitations=(
            "四条可执行案例使用显式 fixture 住宿目录, 不评估实时高德搜索覆盖率或数据新鲜度。",
            "两条阻断案例在评测路由层跳过 Agent, 分别验证缺少房间数和目的地不受支持。",
            "高德住宿 POI 不是 OTA 房价或库存; 本报告不评估价格、房型、评分、设施或可订性。",
            "人工标签只判断区域和已表达偏好的相关性, 不代表住宿质量或用户满意度。",
            "该开发集可用于提示词回归, 不应解释为未见数据上的泛化准确率。",
        ),
    )
