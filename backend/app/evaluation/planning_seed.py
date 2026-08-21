import hashlib
import json
from pathlib import Path

from app.domain.candidates import CandidatePOI
from app.domain.sources import DataMode
from app.domain.travel_data import RouteLeg, WeatherRisk
from app.evaluation.contracts import (
    EvaluationCheck,
    ExpectedPOISearchCall,
    PlanningSeedBaselineReport,
    PlanningSeedCase,
    PlanningSeedCaseResult,
    PlanningSeedManifest,
    PlanningSeedProviderSpec,
    SeedProviderBehavior,
    expected_rate,
)
from app.planning import run_minimal_planning_graph
from app.providers.errors import ProviderRequestError
from app.providers.ports import POISearchRequest, RouteRequest, WeatherRiskRequest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PLANNING_SEED_DIRECTORY = REPOSITORY_ROOT / "evals" / "cases" / "planning-seed"
PLANNING_SEED_MANIFEST_PATH = PLANNING_SEED_DIRECTORY / "manifest.json"
PLANNING_SEED_REPORT_PATH = (
    REPOSITORY_ROOT / "evals" / "reports" / "minimal-planning-graph-baseline.v1.json"
)


class PlanningSeedEvaluationError(RuntimeError):
    """Raised when a scenario provider is called outside the declared case contract."""


class ScenarioTravelDataProvider:
    def __init__(self, spec: PlanningSeedProviderSpec) -> None:
        self._spec = spec
        self.calls: list[ExpectedPOISearchCall] = []

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        call = ExpectedPOISearchCall.model_validate(request.model_dump(mode="json"))
        call_index = len(self.calls)
        if self._spec.behavior == SeedProviderBehavior.FORBIDDEN:
            raise PlanningSeedEvaluationError("provider call was forbidden by the seed case")
        if call_index >= len(self._spec.expected_calls):
            raise PlanningSeedEvaluationError("provider received more calls than declared")
        expected_call = self._spec.expected_calls[call_index]
        if call != expected_call:
            raise PlanningSeedEvaluationError(
                "provider call mismatch at index "
                f"{call_index}: expected {expected_call}, got {call}"
            )
        self.calls.append(call)
        if self._spec.behavior == SeedProviderBehavior.FAILURE:
            assert self._spec.failure is not None
            raise ProviderRequestError(self._spec.failure)
        return self._spec.candidates

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        raise PlanningSeedEvaluationError(f"unexpected weather call for {request.city_adcode}")

    async def get_route(self, request: RouteRequest) -> RouteLeg:
        raise PlanningSeedEvaluationError(f"unexpected route call for {request.city_adcode}")

    def verify_complete(self) -> None:
        if tuple(self.calls) != self._spec.expected_calls:
            raise PlanningSeedEvaluationError("provider did not receive every declared call")


def load_planning_seed_suite(
    manifest_path: Path = PLANNING_SEED_MANIFEST_PATH,
) -> tuple[PlanningSeedManifest, tuple[PlanningSeedCase, ...]]:
    manifest = PlanningSeedManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    case_directory = manifest_path.parent.resolve()
    cases: list[PlanningSeedCase] = []
    for entry in manifest.cases:
        case_path = (case_directory / entry.path).resolve()
        if not case_path.is_relative_to(case_directory):
            raise PlanningSeedEvaluationError("manifest case path leaves the case directory")
        case = PlanningSeedCase.model_validate_json(case_path.read_text(encoding="utf-8"))
        if case.case_id != entry.case_id or case.tier != entry.tier:
            raise PlanningSeedEvaluationError("manifest entry does not match its case file")
        cases.append(case)
    if len({case.request.request_id for case in cases}) != len(cases):
        raise PlanningSeedEvaluationError("seed request ids must be unique")
    return manifest, tuple(cases)


def planning_seed_dataset_sha256(cases: tuple[PlanningSeedCase, ...]) -> str:
    canonical = json.dumps(
        [case.model_dump(mode="json") for case in cases],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def evaluate_planning_seed_case(seed_case: PlanningSeedCase) -> PlanningSeedCaseResult:
    provider = ScenarioTravelDataProvider(seed_case.provider)
    result = await run_minimal_planning_graph(
        seed_case.request,
        provider,
        data_mode=DataMode.FIXTURE,
    )
    provider.verify_complete()

    context = result.planner_context
    expected = seed_case.expected
    hard_ids = tuple(item.constraint_id for item in context.confirmed_hard_constraints)
    soft_ids = tuple(item.constraint_id for item in context.confirmed_soft_constraints)
    pending_ids = tuple(item.constraint_id for item in context.pending_constraints)
    actual_candidate_names = tuple(item.name for item in result.candidates)
    traceable_candidate_count = sum(
        bool(candidate.source.provider and candidate.source.provider_id)
        and candidate.source.data_mode == DataMode.FIXTURE
        for candidate in result.candidates
    )
    checks = (
        EvaluationCheck(code="result_contract_valid", passed=True),
        EvaluationCheck(code="status_matches", passed=result.status == expected.status),
        EvaluationCheck(code="readiness_matches", passed=context.readiness == expected.readiness),
        EvaluationCheck(
            code="ready_capabilities_match",
            passed=context.ready_capabilities == expected.ready_capabilities,
        ),
        EvaluationCheck(
            code="blocked_capabilities_match",
            passed=context.blocked_capabilities == expected.blocked_capabilities,
        ),
        EvaluationCheck(
            code="confirmed_hard_constraints_match",
            passed=hard_ids == expected.constraint_buckets.confirmed_hard,
        ),
        EvaluationCheck(
            code="confirmed_soft_constraints_match",
            passed=soft_ids == expected.constraint_buckets.confirmed_soft,
        ),
        EvaluationCheck(
            code="pending_constraints_match",
            passed=pending_ids == expected.constraint_buckets.pending,
        ),
        EvaluationCheck(
            code="candidate_count_matches",
            passed=len(result.candidates) == expected.candidate_count,
        ),
        EvaluationCheck(
            code="candidate_names_match",
            passed=actual_candidate_names == expected.candidate_names,
        ),
        EvaluationCheck(
            code="provider_calls_match",
            passed=tuple(provider.calls) == seed_case.provider.expected_calls,
        ),
        EvaluationCheck(
            code="candidate_sources_traceable",
            passed=traceable_candidate_count == len(result.candidates),
        ),
    )
    return PlanningSeedCaseResult(
        case_id=seed_case.case_id,
        tier=seed_case.tier,
        passed=all(check.passed for check in checks),
        expected_status=expected.status,
        actual_status=result.status,
        provider_call_count=len(provider.calls),
        candidate_count=len(result.candidates),
        traceable_candidate_count=traceable_candidate_count,
        checks=checks,
    )


async def evaluate_planning_seed_suite(
    manifest_path: Path = PLANNING_SEED_MANIFEST_PATH,
) -> PlanningSeedBaselineReport:
    manifest, cases = load_planning_seed_suite(manifest_path)
    results = tuple([await evaluate_planning_seed_case(case) for case in cases])
    checks = [check for result in results for check in result.checks]
    passed_case_count = sum(result.passed for result in results)
    passed_check_count = sum(check.passed for check in checks)
    candidate_count = sum(result.candidate_count for result in results)
    traceable_candidate_count = sum(result.traceable_candidate_count for result in results)
    return PlanningSeedBaselineReport(
        dataset_sha256=planning_seed_dataset_sha256(cases),
        passed_case_count=passed_case_count,
        case_pass_rate=expected_rate(passed_case_count, len(results)),
        check_count=len(checks),
        passed_check_count=passed_check_count,
        check_pass_rate=expected_rate(passed_check_count, len(checks)),
        candidate_count=candidate_count,
        traceable_candidate_count=traceable_candidate_count,
        source_traceability_rate=expected_rate(traceable_candidate_count, candidate_count),
        results=results,
        limitations=(
            "输入是已结构化 TripRequest, 本报告不评估中文需求抽取质量。",
            "provider 全部为显式 fixture/scenario, 本报告不评估实时数据覆盖或 SLA。",
            "当前 Graph 没有模型 Agent、候选排序、预算校验或逐日行程生成。",
            f"数据集库存固定为 {len(manifest.cases)} 条, 不能外推为生产旅行规划质量。",
        ),
    )
