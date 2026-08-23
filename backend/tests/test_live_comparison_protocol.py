import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.evaluation import (
    COMPARISON_ARMS,
    LiveComparisonPilotProfile,
    LiveComparisonPilotSuite,
    LiveComparisonProtocolError,
    TravelerProfile,
    build_live_comparison_preflight,
    live_comparison_pilot_dataset_sha256,
    load_live_comparison_pilot_suite,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_live_pilot_freezes_three_cases_two_repetitions_and_hard_call_cap() -> None:
    suite = load_live_comparison_pilot_suite()

    assert suite.arms == COMPARISON_ARMS
    assert suite.dataset_role == "repeated_development_pilot"
    assert suite.provider_mode == "frozen_fixture_catalogs"
    assert (suite.model_provider, suite.model_name, suite.temperature) == (
        "deepseek",
        "deepseek-v4-pro",
        0,
    )
    assert suite.repetitions_per_case == 2
    assert {item.profile for item in suite.cases} == set(LiveComparisonPilotProfile)
    assert {item.traveler_profile for item in suite.cases} == {
        TravelerProfile.ADULTS,
        TravelerProfile.COUPLE,
        TravelerProfile.FAMILY_WITH_CHILD,
    }
    assert (
        suite.call_budget.trial_count,
        suite.call_budget.base_model_calls_per_trial,
        suite.call_budget.max_model_calls,
        suite.call_budget.base_completion_tokens_per_trial,
        suite.call_budget.max_completion_tokens,
    ) == (6, 7, 54, 6900, 55800)


def test_live_pilot_hash_covers_suite_and_referenced_provider_datasets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = load_live_comparison_pilot_suite()
    original_hash = live_comparison_pilot_dataset_sha256(suite)
    payload = suite.model_dump(mode="json")
    payload["cases"][0]["title"] = "修改后的 pilot 标题"

    assert (
        live_comparison_pilot_dataset_sha256(LiveComparisonPilotSuite.model_validate(payload))
        != original_hash
    )

    monkeypatch.setattr(
        "app.evaluation.live_comparison.explore_agent_dataset_sha256",
        lambda _suite: "referenced-explore-fixtures-changed",
    )
    assert live_comparison_pilot_dataset_sha256(suite) != original_hash


def test_live_pilot_contract_rejects_unfair_or_unbounded_protocol() -> None:
    payload = load_live_comparison_pilot_suite().model_dump(mode="json")
    payload["fairness"]["product_initial_draft_shared_between_product_arms"] = False
    with pytest.raises(ValidationError):
        LiveComparisonPilotSuite.model_validate(payload)

    payload = load_live_comparison_pilot_suite().model_dump(mode="json")
    payload["call_budget"]["max_model_calls"] = 55
    with pytest.raises(ValidationError, match="max_model_calls"):
        LiveComparisonPilotSuite.model_validate(payload)


def test_live_pilot_loader_rejects_nonready_source_case(tmp_path: Path) -> None:
    payload = load_live_comparison_pilot_suite().model_dump(mode="json")
    payload["cases"][0]["source_plan_case_id"] = "plan-agent-route-timeout-skip-v1"
    suite_path = tmp_path / "invalid-live-pilot.json"
    suite_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(LiveComparisonProtocolError, match="ready Plan Agent case"):
        load_live_comparison_pilot_suite(suite_path)


def test_live_preflight_reports_readiness_without_serializing_secrets() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("deepseek-secret-test-value"),
        deepseek_model="deepseek-v4-pro",
        langsmith_api_key=SecretStr("langsmith-secret-test-value"),
        langsmith_tracing=True,
    )

    preflight = build_live_comparison_preflight(settings)
    serialized = preflight.model_dump_json()

    assert preflight.ready_for_explicit_live_run is True
    assert preflight.blocking_reasons == ()
    assert preflight.model_matches_suite is True
    assert preflight.amap_calls_planned == 0
    assert preflight.max_model_calls == 54
    assert "deepseek-secret-test-value" not in serialized
    assert "langsmith-secret-test-value" not in serialized


def test_live_preflight_lists_every_missing_dependency() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key=None,
        langsmith_api_key=None,
        langsmith_tracing=False,
    )

    preflight = build_live_comparison_preflight(settings)

    assert preflight.ready_for_explicit_live_run is False
    assert preflight.blocking_reasons == (
        "deepseek_api_key_missing",
        "langsmith_api_key_missing",
        "langsmith_tracing_disabled",
    )


def test_live_preflight_blocks_model_drift() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key=SecretStr("deepseek-secret-test-value"),
        deepseek_model="different-model",
        langsmith_api_key=SecretStr("langsmith-secret-test-value"),
        langsmith_tracing=True,
    )

    preflight = build_live_comparison_preflight(settings)

    assert preflight.ready_for_explicit_live_run is False
    assert preflight.model_matches_suite is False
    assert preflight.blocking_reasons == ("deepseek_model_mismatch",)


def test_committed_live_pilot_schema_matches_contract() -> None:
    schema = json.loads(
        (REPOSITORY_ROOT / "evals" / "schemas" / "live-comparison-pilot-suite.v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema == LiveComparisonPilotSuite.model_json_schema(mode="validation")
