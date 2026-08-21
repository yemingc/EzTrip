import hashlib
import json
from pathlib import Path

from app.agents.contracts import PlannerModelResponse
from app.agents.single_planner import PlannerProposalModel
from app.domain.candidates import CandidatePOI
from app.domain.context import PlannerContext
from app.domain.sources import DataMode
from app.domain.travel_data import RouteLeg, WeatherRisk
from app.evaluation.contracts import EvaluationCheck, ExpectedPOISearchCall, expected_rate
from app.evaluation.vertical_slice_contracts import (
    VerticalSliceCase,
    VerticalSliceCaseResult,
    VerticalSliceGateReport,
    VerticalSlicePOIResponse,
    VerticalSliceSuite,
)
from app.planning import run_trip_planning_vertical_slice
from app.planning.vertical_slice import VerticalSliceResult
from app.providers.ports import POISearchRequest, RouteRequest, WeatherRiskRequest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
VERTICAL_SLICE_SUITE_PATH = (
    REPOSITORY_ROOT / "evals" / "cases" / "beijing-vertical-slice" / "suite.v1.json"
)
VERTICAL_SLICE_REPORT_PATH = (
    REPOSITORY_ROOT / "evals" / "reports" / "beijing-three-day-gate2.v1.json"
)
VERTICAL_SLICE_NORMAL_RESULT_PATH = (
    REPOSITORY_ROOT / "evals" / "reports" / "beijing-three-day-gate2-normal-result.v1.json"
)


class VerticalSliceEvaluationError(RuntimeError):
    """Raised when a Gate 2 fixture is used outside its declared contract."""


class VerticalSliceScenarioProvider:
    def __init__(self, responses: tuple[VerticalSlicePOIResponse, ...]) -> None:
        self._responses = responses
        self.calls: list[ExpectedPOISearchCall] = []

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        call = ExpectedPOISearchCall.model_validate(request.model_dump(mode="json"))
        call_index = len(self.calls)
        if call_index >= len(self._responses):
            raise VerticalSliceEvaluationError("provider received more calls than declared")
        response = self._responses[call_index]
        if call != response.request:
            raise VerticalSliceEvaluationError(
                "provider call mismatch at index "
                f"{call_index}: expected {response.request}, got {call}"
            )
        self.calls.append(call)
        return response.candidates

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        raise VerticalSliceEvaluationError(f"unexpected weather call for {request.city_adcode}")

    async def get_route(self, request: RouteRequest) -> RouteLeg:
        raise VerticalSliceEvaluationError(f"unexpected route call for {request.city_adcode}")

    def verify_complete(self) -> None:
        expected_calls = tuple(response.request for response in self._responses)
        if tuple(self.calls) != expected_calls:
            raise VerticalSliceEvaluationError("provider did not receive every declared call")


class FixturePlannerProposalModel(PlannerProposalModel):
    def __init__(self, response: PlannerModelResponse) -> None:
        self._response = response

    def propose(
        self,
        context: PlannerContext,
        candidates: tuple[CandidatePOI, ...],
    ) -> PlannerModelResponse:
        del context
        candidate_ids = {candidate.candidate_id for candidate in candidates}
        proposal_ids = {item.candidate_id for item in self._response.proposal.items}
        if proposal_ids != candidate_ids:
            raise VerticalSliceEvaluationError(
                "fixture Planner proposal does not match the provider candidate set"
            )
        return self._response


def load_vertical_slice_suite(
    path: Path = VERTICAL_SLICE_SUITE_PATH,
) -> VerticalSliceSuite:
    return VerticalSliceSuite.model_validate_json(path.read_text(encoding="utf-8"))


def vertical_slice_dataset_sha256(suite: VerticalSliceSuite) -> str:
    canonical = json.dumps(
        suite.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def run_vertical_slice_case(
    case: VerticalSliceCase,
) -> tuple[VerticalSliceResult, tuple[ExpectedPOISearchCall, ...]]:
    provider = VerticalSliceScenarioProvider(case.provider_responses)
    model = FixturePlannerProposalModel(
        PlannerModelResponse(
            proposal=case.planner_proposal,
            model=case.planner_model,
            latency_ms=0,
        )
    )
    result = await run_trip_planning_vertical_slice(
        case.request,
        provider,
        model,
        case.cost_items,
        data_mode=DataMode.FIXTURE,
    )
    provider.verify_complete()
    return result, tuple(provider.calls)


def _is_traceable(candidate: CandidatePOI) -> bool:
    return bool(
        candidate.source.provider
        and candidate.source.provider_id
        and candidate.source.data_mode == DataMode.FIXTURE
    )


async def evaluate_vertical_slice_case(case: VerticalSliceCase) -> VerticalSliceCaseResult:
    first, provider_calls = await run_vertical_slice_case(case)
    replay, replay_provider_calls = await run_vertical_slice_case(case)
    deterministic_replay_match = first == replay and provider_calls == replay_provider_calls

    candidates = first.upstream.candidates
    scheduled_items = tuple(item for day in first.plan.days for item in day.items)
    candidate_ids = {candidate.candidate_id for candidate in candidates}
    scheduled_ids = {item.candidate_id for item in scheduled_items}
    traceable_candidates = tuple(candidate for candidate in candidates if _is_traceable(candidate))
    traceable_items = tuple(
        item
        for item in scheduled_items
        if item.source is not None
        and item.source.provider
        and item.source.provider_id
        and item.source.data_mode == DataMode.FIXTURE
    )
    expected_calls = tuple(response.request for response in case.provider_responses)
    expected_dates = tuple(day.date for day in first.upstream.planner_context.days)
    issue_rule_codes = tuple(item.rule_code for item in first.validation.issues)
    confirmed_must_visit_ids = {
        item.constraint_id
        for item in first.upstream.planner_context.confirmed_hard_constraints
        if item.kind.value == "must_visit"
    }
    query_constraint_ids = {
        query.source_constraint_id for query in first.upstream.candidate_queries
    }
    budget_total_matches = (
        first.validation.budget.total_minimum == first.plan.total_cost_minimum
        and first.validation.budget.total_maximum == first.plan.total_cost_maximum
    )

    checks = (
        EvaluationCheck(
            code="provider_calls_match",
            passed=provider_calls == expected_calls,
        ),
        EvaluationCheck(
            code="request_constraints_preserved",
            passed=confirmed_must_visit_ids == query_constraint_ids
            and len(confirmed_must_visit_ids) == len(case.provider_responses),
        ),
        EvaluationCheck(
            code="structured_three_day_plan",
            passed=len(first.plan.days) == 3
            and tuple(day.date for day in first.plan.days) == expected_dates
            and all(day.items for day in first.plan.days),
        ),
        EvaluationCheck(
            code="candidate_coverage",
            passed=scheduled_ids == candidate_ids and len(scheduled_items) == len(candidates),
        ),
        EvaluationCheck(
            code="recommendation_sources_traceable",
            passed=len(traceable_candidates) == len(candidates)
            and len(traceable_items) == len(scheduled_items),
        ),
        EvaluationCheck(code="budget_recomputed_from_cost_items", passed=budget_total_matches),
        EvaluationCheck(
            code="expected_validation_outcome",
            passed=first.outcome == case.expected.outcome
            and first.validation.status == case.expected.validation_status
            and first.validation.budget.status == case.expected.budget_status
            and first.validation.can_finalize == case.expected.can_finalize
            and issue_rule_codes == case.expected.issue_rule_codes,
        ),
        EvaluationCheck(
            code="no_silent_constraint_relaxation",
            passed=candidate_ids == scheduled_ids
            and confirmed_must_visit_ids == query_constraint_ids,
        ),
        EvaluationCheck(
            code="plan_not_auto_finalized",
            passed=first.plan.status.value == "draft",
        ),
        EvaluationCheck(
            code="fixture_model_replay_is_deterministic",
            passed=deterministic_replay_match and first.planner.model == "fixture-planner-gate2-v1",
        ),
    )
    return VerticalSliceCaseResult(
        case_id=case.case_id,
        passed=all(check.passed for check in checks),
        outcome=first.outcome,
        validation_status=first.validation.status,
        budget_status=first.validation.budget.status,
        can_finalize=first.validation.can_finalize,
        provider_call_count=len(provider_calls),
        candidate_count=len(candidates),
        scheduled_candidate_count=len(scheduled_items),
        traceable_candidate_count=len(traceable_candidates),
        day_count=len(first.plan.days),
        budget_total_minimum=first.validation.budget.total_minimum,
        budget_total_maximum=first.validation.budget.total_maximum,
        budget_minimum_gap=first.validation.budget.minimum_gap,
        issue_rule_codes=issue_rule_codes,
        deterministic_replay_match=deterministic_replay_match,
        checks=checks,
    )


async def evaluate_vertical_slice_suite(
    path: Path = VERTICAL_SLICE_SUITE_PATH,
) -> VerticalSliceGateReport:
    suite = load_vertical_slice_suite(path)
    results = tuple([await evaluate_vertical_slice_case(case) for case in suite.cases])
    checks = tuple(check for result in results for check in result.checks)
    candidate_count = sum(result.candidate_count for result in results)
    traceable_count = sum(result.traceable_candidate_count for result in results)
    passed_case_count = sum(result.passed for result in results)
    passed_check_count = sum(check.passed for check in checks)
    return VerticalSliceGateReport(
        dataset_sha256=vertical_slice_dataset_sha256(suite),
        passed_case_count=passed_case_count,
        case_pass_rate=expected_rate(passed_case_count, len(results)),
        check_count=len(checks),
        passed_check_count=passed_check_count,
        check_pass_rate=expected_rate(passed_check_count, len(checks)),
        candidate_count=candidate_count,
        traceable_candidate_count=traceable_count,
        source_traceability_rate=expected_rate(traceable_count, candidate_count),
        deterministic_replay_count=sum(result.deterministic_replay_match for result in results),
        results=results,
        limitations=(
            "输入是版本化 TripRequest, 本报告不评估中文字段抽取质量。",
            "POI 与费用均为显式 fixture, 不代表实时票价、营业时间或市场价格。",
            "Planner 使用固定注入提案以验证可重放主链, 本报告不衡量模型规划质量。",
            "当前没有路线、天气、酒店可订状态、开放式推荐、HITL 执行或多 Agent 对照。",
        ),
    )
