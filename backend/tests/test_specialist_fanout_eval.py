import asyncio

import pytest
from pydantic import ValidationError

from app.domain.sources import DataMode
from app.evaluation import (
    FixtureExploreModel,
    FixtureStayModel,
    evaluate_specialist_fanout_case,
    evaluate_specialist_fanout_suite,
    load_specialist_fanout_suite,
    specialist_fanout_dataset_sha256,
)
from app.planning import run_specialist_fanout


def test_specialist_fanout_suite_contract_and_dataset_hash_are_stable() -> None:
    suite = load_specialist_fanout_suite()

    assert len(suite.cases) == 5
    assert len({item.case_id for item in suite.cases}) == 5
    assert len({item.request.request_id for item in suite.cases}) == 5
    assert len(specialist_fanout_dataset_sha256(suite)) == 64


def test_fixture_specialist_fanout_suite_proves_orchestration_contracts() -> None:
    explore_model = FixtureExploreModel()
    stay_model = FixtureStayModel()

    report = asyncio.run(
        evaluate_specialist_fanout_suite(
            lambda request, provider: run_specialist_fanout(
                request,
                provider,
                explore_model,
                stay_model,
                data_mode=DataMode.FIXTURE,
            ),
            execution_mode="fixture",
            explore_model="fixture-explore-fanout-model",
            stay_model="fixture-stay-fanout-model",
        )
    )

    assert report.passed_case_count == report.case_count == 5
    assert report.branch_status_match_count == report.branch_expectation_count == 15
    assert report.exact_ordered_merge_case_count == 5
    assert report.typed_provider_failure_count == 2
    assert report.preserved_success_count == 4
    assert report.proactive_weather_call_count == 4
    assert report.blocked_zero_call_case_count == 1
    assert report.parallel_provider_entry_case_count == 1
    assert report.source_traceability_case_count == 4
    assert report.model_call_count == 13
    assert report.provider_call_count == 11
    assert report.total_tokens == 0


def test_suite_rejects_failure_injection_that_does_not_match_branch() -> None:
    suite = load_specialist_fanout_suite()
    payload = suite.model_dump(mode="json")
    payload["cases"][1]["injected_provider_failure"] = "weather"

    with pytest.raises(ValidationError, match="must match the failed branch"):
        type(suite).model_validate(payload)


def test_unexpected_runner_error_is_redacted_to_stable_code() -> None:
    case = load_specialist_fanout_suite().cases[0]

    async def fail_runner(*_: object) -> object:
        raise RuntimeError("secret upstream body")

    result = asyncio.run(evaluate_specialist_fanout_case(case, fail_runner))  # type: ignore[arg-type]

    assert result.passed is False
    assert result.error_code is not None
    assert "secret" not in result.error_code
    assert all(check.passed is False for check in result.checks)
