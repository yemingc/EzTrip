import asyncio
import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain import (
    BudgetCategory,
    BudgetConstraint,
    CandidatePOI,
    ConstraintSet,
    MinimalPlanningResult,
    Party,
    PlannerCapability,
    PlannerReadiness,
    PlanningNodeName,
    PlanningNodeOutcome,
    PlanningWorkflowStatus,
    ProviderErrorCategory,
    ProviderFailure,
    TripRequest,
)
from app.domain.sources import DataMode
from app.domain.travel_data import RouteLeg, WeatherRisk
from app.planning import (
    PlanningGraphProtocolError,
    build_minimal_planning_graph,
    build_planning_run_config,
    compile_planner_context,
    derive_candidate_search_queries,
    run_minimal_planning_graph,
)
from app.providers import (
    POISearchRequest,
    RouteRequest,
    WeatherRiskRequest,
    load_fixture_amap_provider,
)
from app.providers.errors import ProviderRequestError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUEST_EXAMPLE_PATH = REPOSITORY_ROOT / "docs" / "contracts" / "examples" / "trip-request.v1.json"


def load_request() -> TripRequest:
    return TripRequest.model_validate_json(REQUEST_EXAMPLE_PATH.read_text(encoding="utf-8"))


def run_fixture(request: TripRequest) -> MinimalPlanningResult:
    return asyncio.run(
        run_minimal_planning_graph(
            request,
            load_fixture_amap_provider(),
            data_mode=DataMode.FIXTURE,
        )
    )


class SearchGuardProvider:
    def __init__(
        self,
        failure: ProviderFailure | None = None,
        *,
        return_empty: bool = False,
    ) -> None:
        self.failure = failure
        self.return_empty = return_empty
        self.search_calls: list[POISearchRequest] = []

    async def search_pois(self, request: POISearchRequest) -> tuple[CandidatePOI, ...]:
        self.search_calls.append(request)
        if self.failure is not None:
            raise ProviderRequestError(self.failure)
        if self.return_empty:
            return ()
        raise AssertionError("search_pois was not expected")

    async def get_weather_risks(
        self,
        request: WeatherRiskRequest,
    ) -> tuple[WeatherRisk, ...]:
        raise AssertionError(f"weather was not expected: {request.city_adcode}")

    async def get_route(self, request: RouteRequest) -> RouteLeg:
        raise AssertionError(f"route was not expected: {request.city_adcode}")


def test_normal_request_runs_three_named_nodes_and_returns_traceable_fixture_candidate() -> None:
    request = load_request()
    provider = load_fixture_amap_provider()
    graph = build_minimal_planning_graph(provider)

    node_names = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    result = asyncio.run(run_minimal_planning_graph(request, provider, data_mode=DataMode.FIXTURE))

    assert node_names == {
        "compile_context",
        "clarification_gate",
        "candidate_search",
    }
    assert result.status == PlanningWorkflowStatus.CANDIDATES_READY
    assert result.data_mode == DataMode.FIXTURE
    assert result.planner_context.readiness == PlannerReadiness.READY
    assert result.candidate_queries[0].requested_value == "故宫"
    assert result.candidate_queries[0].keywords == "故宫博物院"
    assert result.candidate_queries[0].source_constraint_id == "must_visit_forbidden_city"
    assert [candidate.name for candidate in result.candidates] == ["故宫博物院"]
    assert result.candidates[0].source.provider == "amap"
    assert result.candidates[0].source.provider_id == "B000A8UIN8"
    assert result.candidates[0].source.data_mode == DataMode.FIXTURE
    assert [event.node for event in result.events] == [
        PlanningNodeName.COMPILE_CONTEXT,
        PlanningNodeName.CLARIFICATION_GATE,
        PlanningNodeName.CANDIDATE_SEARCH,
    ]
    assert [event.outcome for event in result.events] == [
        PlanningNodeOutcome.COMPILED,
        PlanningNodeOutcome.ALLOWED,
        PlanningNodeOutcome.SUCCEEDED,
    ]


def test_unsupported_destination_stops_at_gate_without_calling_provider() -> None:
    request = load_request().model_copy(update={"destination_city": "南京市"})
    provider = SearchGuardProvider()

    result = asyncio.run(
        run_minimal_planning_graph(
            request,
            provider,
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == PlanningWorkflowStatus.NEEDS_CLARIFICATION
    assert result.planner_context.readiness == PlannerReadiness.NEEDS_CLARIFICATION
    assert PlannerCapability.CANDIDATE_SEARCH in result.planner_context.blocked_capabilities
    assert result.candidate_queries == ()
    assert result.candidates == ()
    assert provider.search_calls == []
    assert [event.outcome for event in result.events] == [
        PlanningNodeOutcome.COMPILED,
        PlanningNodeOutcome.BLOCKED,
    ]


def test_candidate_query_derivation_rejects_unsupported_city_when_called_out_of_route() -> None:
    context = compile_planner_context(
        load_request().model_copy(update={"destination_city": "南京市"})
    )

    with pytest.raises(PlanningGraphProtocolError, match="requires a supported city"):
        derive_candidate_search_queries(context)


def test_missing_budget_is_nonblocking_for_candidate_search() -> None:
    result = run_fixture(load_request().model_copy(update={"budget": None}))

    assert result.status == PlanningWorkflowStatus.CANDIDATES_READY
    assert result.planner_context.readiness == PlannerReadiness.READY_WITH_QUESTIONS
    assert PlannerCapability.BUDGET_VALIDATION in result.planner_context.blocked_capabilities
    assert PlannerCapability.CANDIDATE_SEARCH in result.planner_context.ready_capabilities
    assert result.candidates[0].name == "故宫博物院"


def test_missing_rooms_can_block_finalization_without_blocking_candidate_search() -> None:
    request = load_request()
    assert request.budget is not None
    lodging_budget = BudgetConstraint(
        total_limit=request.budget.total_limit,
        included_categories=(BudgetCategory.LODGING, *request.budget.included_categories),
    )
    request = request.model_copy(
        update={
            "party": Party(adults=2, rooms=None),
            "budget": lodging_budget,
        }
    )

    result = run_fixture(request)

    assert result.status == PlanningWorkflowStatus.CANDIDATES_READY
    assert result.planner_context.readiness == PlannerReadiness.NEEDS_CLARIFICATION
    assert PlannerCapability.PLAN_FINALIZATION in result.planner_context.blocked_capabilities
    assert PlannerCapability.CANDIDATE_SEARCH in result.planner_context.ready_capabilities


def test_no_confirmed_must_visit_skips_open_ended_recommendation() -> None:
    request = load_request().model_copy(update={"constraints": ConstraintSet()})
    provider = SearchGuardProvider()

    result = asyncio.run(
        run_minimal_planning_graph(
            request,
            provider,
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == PlanningWorkflowStatus.NO_CANDIDATE_QUERY
    assert result.candidate_queries == ()
    assert result.candidates == ()
    assert provider.search_calls == []
    assert result.events[-1].outcome == PlanningNodeOutcome.SKIPPED


@pytest.mark.parametrize("return_empty", [False, True])
def test_provider_failures_are_recorded_without_fabricating_candidates(
    return_empty: bool,
) -> None:
    failure = None
    if not return_empty:
        failure = ProviderFailure(
            provider="amap",
            operation="maps_text_search",
            category=ProviderErrorCategory.TIMEOUT,
            message="injected timeout",
            retryable=True,
        )
    provider = SearchGuardProvider(failure, return_empty=return_empty)

    result = asyncio.run(
        run_minimal_planning_graph(
            load_request(),
            provider,
            data_mode=DataMode.FIXTURE,
        )
    )

    assert result.status == PlanningWorkflowStatus.PROVIDER_FAILED
    assert result.candidates == ()
    assert len(result.provider_failures) == 1
    expected_category = (
        ProviderErrorCategory.EMPTY_RESULT if return_empty else ProviderErrorCategory.TIMEOUT
    )
    assert result.provider_failures[0].category == expected_category
    assert result.events[-1].outcome == PlanningNodeOutcome.FAILED


def test_trace_config_contains_versions_and_no_raw_user_text() -> None:
    request = load_request()
    config = build_planning_run_config(request, data_mode=DataMode.FIXTURE)

    assert config["run_name"] == "eztrip-minimal-planning-graph-v1"
    assert config["tags"] == ["ez-007", "planning-graph", "fixture"]
    assert config["metadata"] == {
        "workflow_version": "minimal-planning-graph-v1",
        "request_schema_version": "1.0",
        "request_id": request.request_id,
        "data_mode": "fixture",
        "raw_user_text_in_metadata": False,
    }
    assert request.raw_text not in str(config)


def test_result_contract_rejects_candidates_ready_without_candidates() -> None:
    result = run_fixture(load_request())
    payload = result.model_dump(mode="json")

    with pytest.raises(ValidationError, match="requires queries and candidates"):
        MinimalPlanningResult.model_validate({**payload, "candidates": []})


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("request_id", "request_id must match"),
        ("duplicate_query", "candidate query ids must be unique"),
        ("duplicate_candidate", "candidate ids must be unique"),
        ("unknown_constraint", "must reference confirmed must_visit"),
        ("changed_requested_value", "must preserve the constraint value"),
        ("changed_query_city", "candidate query city must match"),
        ("changed_candidate_city", "candidate city must match"),
        ("changed_candidate_mode", "source mode must match"),
        ("wrong_event_prefix", "events must start"),
        ("wrong_compile_outcome", "must record a compiled outcome"),
        ("ready_marked_blocked", "requires candidate search to be blocked"),
        ("ready_wrong_gate_outcome", "must pass clarification_gate"),
        ("ready_wrong_third_node", "third workflow event"),
        ("ready_wrong_search_outcome", "must record a succeeded search"),
        ("ready_with_failure", "without failures"),
        ("blocked_wrong_gate_outcome", "must stop at clarification_gate"),
        ("blocked_with_failure", "cannot contain provider search outputs"),
        ("blocked_marked_no_query", "cannot run while candidate search is blocked"),
        ("no_query_wrong_outcome", "must record a skipped search"),
        ("no_query_with_failure", "cannot contain provider outputs"),
        ("provider_failed_wrong_outcome", "must record a failed search"),
        ("provider_failed_without_failure", "requires queries and typed failures"),
    ],
)
def test_result_contract_rejects_cross_field_conflicts(
    case: str,
    expected_error: str,
) -> None:
    if case.startswith("blocked_"):
        result = asyncio.run(
            run_minimal_planning_graph(
                load_request().model_copy(update={"destination_city": "南京市"}),
                SearchGuardProvider(),
                data_mode=DataMode.FIXTURE,
            )
        )
    elif case.startswith("no_query_"):
        result = asyncio.run(
            run_minimal_planning_graph(
                load_request().model_copy(update={"constraints": ConstraintSet()}),
                SearchGuardProvider(),
                data_mode=DataMode.FIXTURE,
            )
        )
    elif case.startswith("provider_failed_"):
        failure = ProviderFailure(
            provider="amap",
            operation="maps_text_search",
            category=ProviderErrorCategory.TIMEOUT,
            message="injected timeout",
            retryable=True,
        )
        result = asyncio.run(
            run_minimal_planning_graph(
                load_request(),
                SearchGuardProvider(failure),
                data_mode=DataMode.FIXTURE,
            )
        )
    else:
        result = run_fixture(load_request())

    payload = copy.deepcopy(result.model_dump(mode="json"))
    sample_failure = ProviderFailure(
        provider="amap",
        operation="maps_text_search",
        category=ProviderErrorCategory.TIMEOUT,
        message="injected timeout",
        retryable=True,
    ).model_dump(mode="json")

    if case == "request_id":
        payload["request_id"] = "different_request"
    elif case == "duplicate_query":
        payload["candidate_queries"].append(copy.deepcopy(payload["candidate_queries"][0]))
    elif case == "duplicate_candidate":
        payload["candidates"].append(copy.deepcopy(payload["candidates"][0]))
    elif case == "unknown_constraint":
        payload["candidate_queries"][0]["source_constraint_id"] = "unknown_constraint"
    elif case == "changed_requested_value":
        payload["candidate_queries"][0]["requested_value"] = "天坛"
    elif case == "changed_query_city":
        payload["candidate_queries"][0]["city_adcode"] = "310000"
    elif case == "changed_candidate_city":
        payload["candidates"][0]["city"] = "上海市"
    elif case == "changed_candidate_mode":
        payload["candidates"][0]["source"]["data_mode"] = "live"
    elif case == "wrong_event_prefix":
        payload["events"][0]["node"] = "candidate_search"
    elif case == "wrong_compile_outcome":
        payload["events"][0]["outcome"] = "allowed"
    elif case == "ready_marked_blocked":
        payload["status"] = "needs_clarification"
    elif case == "ready_wrong_gate_outcome":
        payload["events"][1]["outcome"] = "blocked"
    elif case == "ready_wrong_third_node":
        payload["events"][2]["node"] = "clarification_gate"
    elif case == "ready_wrong_search_outcome":
        payload["events"][2]["outcome"] = "failed"
    elif case == "ready_with_failure":
        payload["provider_failures"] = [sample_failure]
    elif case == "blocked_wrong_gate_outcome":
        payload["events"][1]["outcome"] = "allowed"
    elif case == "blocked_with_failure":
        payload["provider_failures"] = [sample_failure]
    elif case == "blocked_marked_no_query":
        payload["status"] = "no_candidate_query"
    elif case == "no_query_wrong_outcome":
        payload["events"][2]["outcome"] = "succeeded"
    elif case == "no_query_with_failure":
        payload["provider_failures"] = [sample_failure]
    elif case == "provider_failed_wrong_outcome":
        payload["events"][2]["outcome"] = "succeeded"
    elif case == "provider_failed_without_failure":
        payload["provider_failures"] = []
    else:
        raise AssertionError(f"unknown test case: {case}")

    with pytest.raises(ValidationError, match=expected_error):
        MinimalPlanningResult.model_validate(payload)
