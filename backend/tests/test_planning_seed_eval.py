import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.domain.sources import DataMode
from app.evaluation.contracts import (
    ExpectedPOISearchCall,
    PlanningSeedBaselineReport,
    PlanningSeedCase,
    PlanningSeedManifest,
    SeedProviderBehavior,
    SeedTier,
)
from app.evaluation.planning_seed import (
    PLANNING_SEED_DIRECTORY,
    PLANNING_SEED_MANIFEST_PATH,
    PLANNING_SEED_REPORT_PATH,
    PlanningSeedEvaluationError,
    ScenarioTravelDataProvider,
    evaluate_planning_seed_suite,
    load_planning_seed_suite,
)
from app.providers import POISearchRequest
from scripts.export_planning_seed_schema import (
    DEFAULT_OUTPUT_PATH as PLANNING_SEED_SCHEMA_PATH,
)
from scripts.export_planning_seed_schema import build_planning_seed_schema


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_manifest_freezes_six_standard_and_four_hard_unique_requests() -> None:
    manifest, cases = load_planning_seed_suite()

    assert manifest.suite == "planning-seed-v1"
    assert manifest.status == "executable_baseline"
    assert len(cases) == 10
    assert sum(case.tier == SeedTier.STANDARD for case in cases) == 6
    assert sum(case.tier == SeedTier.HARD for case in cases) == 4
    assert len({case.case_id for case in cases}) == 10
    assert len({case.request.request_id for case in cases}) == 10
    declared_files = {entry.path for entry in manifest.cases}
    actual_files = {
        path.name for path in PLANNING_SEED_DIRECTORY.glob("*.json") if path.name != "manifest.json"
    }
    assert actual_files == declared_files


def test_every_seed_case_matches_the_generated_json_schema() -> None:
    committed_schema = load_json(PLANNING_SEED_SCHEMA_PATH)
    assert committed_schema == build_planning_seed_schema()
    Draft202012Validator.check_schema(committed_schema)
    validator = Draft202012Validator(committed_schema)
    manifest = PlanningSeedManifest.model_validate_json(
        PLANNING_SEED_MANIFEST_PATH.read_text(encoding="utf-8")
    )

    for entry in manifest.cases:
        payload = load_json(PLANNING_SEED_DIRECTORY / entry.path)
        errors = sorted(
            validator.iter_errors(payload),
            key=lambda error: "/".join(str(part) for part in error.absolute_path),
        )
        assert not errors, "\n".join(error.message for error in errors)
        case = PlanningSeedCase.model_validate(payload)
        assert case.case_id == entry.case_id
        assert case.tier == entry.tier


def test_seed_inventory_covers_china_user_and_current_graph_boundaries() -> None:
    _, cases = load_planning_seed_suite()

    assert {case.request.destination_city for case in cases} >= {
        "北京市",
        "上海",
        "成都市",
        "南京市",
    }
    assert any(case.request.party.children for case in cases)
    assert any(case.request.party.seniors for case in cases)
    assert any(case.request.budget is None for case in cases)
    assert any(
        case.request.budget and case.request.budget.total_limit == Decimal("500.00")
        for case in cases
    )
    assert {case.provider.behavior for case in cases} == set(SeedProviderBehavior)
    assert {case.expected.status for case in cases} == {
        "needs_clarification",
        "no_candidate_query",
        "candidates_ready",
        "provider_failed",
    }
    assert all(case.request.locale == "zh-CN" for case in cases)
    assert all(case.boundary_notes for case in cases)
    assert all(case.future_expectations for case in cases)


def test_baseline_report_is_reproducible_and_all_deterministic_checks_pass() -> None:
    committed_report = PlanningSeedBaselineReport.model_validate(
        load_json(PLANNING_SEED_REPORT_PATH)
    )
    replayed_report = asyncio.run(evaluate_planning_seed_suite())

    assert committed_report == replayed_report
    assert replayed_report.passed_case_count == 10
    assert replayed_report.case_pass_rate == Decimal("1.0000")
    assert replayed_report.passed_check_count == 120
    assert replayed_report.check_count == 120
    assert replayed_report.check_pass_rate == Decimal("1.0000")
    assert replayed_report.traceable_candidate_count == 6
    assert replayed_report.candidate_count == 6
    assert replayed_report.source_traceability_rate == Decimal("1.0000")
    assert len(replayed_report.dataset_sha256) == 64
    assert {result.actual_status for result in replayed_report.results} == {
        "needs_clarification",
        "no_candidate_query",
        "candidates_ready",
        "provider_failed",
    }


def test_scenario_provider_rejects_a_call_outside_the_case_contract() -> None:
    _, cases = load_planning_seed_suite()
    success_case = next(
        case for case in cases if case.provider.behavior == SeedProviderBehavior.SUCCESS
    )
    provider = ScenarioTravelDataProvider(success_case.provider)

    with pytest.raises(PlanningSeedEvaluationError, match="call mismatch"):
        asyncio.run(
            provider.search_pois(
                POISearchRequest(keywords="未声明景点", city_adcode="110000", limit=1)
            )
        )
    with pytest.raises(PlanningSeedEvaluationError, match="did not receive every"):
        provider.verify_complete()


def test_scenario_candidates_are_explicit_fixture_data() -> None:
    _, cases = load_planning_seed_suite()
    candidates = [candidate for case in cases for candidate in case.provider.candidates]

    assert candidates
    assert all(candidate.source.data_mode == DataMode.FIXTURE for candidate in candidates)
    assert all(candidate.source.provider_id for candidate in candidates)
    assert {candidate.source.provider for candidate in candidates} == {"amap", "eval_fixture"}


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("wrong_case_id", "must encode the tier and version"),
        ("wrong_constraint_bucket", "must match the TripRequest"),
        ("success_wrong_status", "must expect candidates_ready"),
        ("success_wrong_candidate_name", "must match provider candidates"),
        ("forbidden_wrong_status", "must stop or skip"),
    ],
)
def test_seed_case_contract_rejects_cross_field_conflicts(
    case: str,
    expected_error: str,
) -> None:
    manifest, cases = load_planning_seed_suite()
    del manifest
    selected = cases[0]
    if case == "forbidden_wrong_status":
        selected = next(
            item for item in cases if item.provider.behavior == SeedProviderBehavior.FORBIDDEN
        )
    payload = copy.deepcopy(selected.model_dump(mode="json"))

    if case == "wrong_case_id":
        payload["case_id"] = "seed-hard-wrong-tier-v1"
    elif case == "wrong_constraint_bucket":
        payload["expected"]["constraint_buckets"]["confirmed_hard"] = []
    elif case == "success_wrong_status":
        payload["expected"]["status"] = "provider_failed"
    elif case == "success_wrong_candidate_name":
        payload["expected"]["candidate_names"] = ["错误候选"]
    elif case == "forbidden_wrong_status":
        payload["expected"]["status"] = "candidates_ready"
    else:
        raise AssertionError(f"unknown case: {case}")

    with pytest.raises(ValidationError, match=expected_error):
        PlanningSeedCase.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("passed_case_count", 9, "passed_case_count"),
        ("case_pass_rate", "0.9000", "case_pass_rate"),
        ("check_pass_rate", "0.9000", "check_pass_rate"),
        ("source_traceability_rate", "0.5000", "source_traceability_rate"),
    ],
)
def test_report_contract_rejects_inconsistent_aggregates(
    field: str,
    value: object,
    expected_error: str,
) -> None:
    payload = load_json(PLANNING_SEED_REPORT_PATH)
    payload[field] = value

    with pytest.raises(ValidationError, match=expected_error):
        PlanningSeedBaselineReport.model_validate(payload)


def test_expected_provider_call_contract_preserves_exact_arguments() -> None:
    call = ExpectedPOISearchCall(keywords="故宫博物院", city_adcode="110000", limit=1)

    assert call.model_dump() == {
        "keywords": "故宫博物院",
        "city_adcode": "110000",
        "limit": 1,
    }
