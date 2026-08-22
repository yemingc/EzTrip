import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation.weather_repair import (
    evaluate_weather_repair_suite,
    load_weather_repair_suite,
    weather_repair_dataset_sha256,
)
from app.evaluation.weather_repair_contracts import (
    WeatherRepairBaselineReport,
    WeatherRepairEvalSuite,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def fixture_report() -> WeatherRepairBaselineReport:
    return asyncio.run(evaluate_weather_repair_suite())


def test_weather_repair_suite_contract_and_hash_are_stable() -> None:
    suite = load_weather_repair_suite()

    assert len(suite.cases) == 10
    assert len({item.case_id for item in suite.cases}) == 10
    assert len(weather_repair_dataset_sha256(suite)) == 64


def test_fixture_suite_proves_proactive_routing_hitl_and_bounded_retries(
    fixture_report: WeatherRepairBaselineReport,
) -> None:
    assert fixture_report.passed_case_count == fixture_report.case_count == 10
    assert fixture_report.no_false_positive_case_count == 5
    assert fixture_report.proactive_task_case_count == 5
    assert fixture_report.auto_applied_case_count == 1
    assert fixture_report.hitl_case_count == 1
    assert fixture_report.bounded_retry_case_count == 3
    assert fixture_report.source_traceability_rate == 1
    assert fixture_report.deterministic_replay_case_count == 10
    assert fixture_report.coordinator_model_call_count == 0


def test_suite_rejects_missing_weather_scenario() -> None:
    suite = load_weather_repair_suite()
    payload = suite.model_dump(mode="json")
    payload["cases"][0]["scenario"] = payload["cases"][1]["scenario"]

    with pytest.raises(ValidationError, match="one case per scenario"):
        WeatherRepairEvalSuite.model_validate(payload)


def test_report_rejects_drifted_aggregate(
    fixture_report: WeatherRepairBaselineReport,
) -> None:
    payload = fixture_report.model_dump(mode="json")
    payload["proactive_task_case_count"] -= 1

    with pytest.raises(ValidationError, match="must match weather-repair"):
        WeatherRepairBaselineReport.model_validate(payload)


def test_committed_weather_repair_schemas_and_report_match_code(
    fixture_report: WeatherRepairBaselineReport,
) -> None:
    suite_schema = json.loads(
        (REPOSITORY_ROOT / "evals/schemas/weather-repair-suite.v1.json").read_text(encoding="utf-8")
    )
    report_schema = json.loads(
        (REPOSITORY_ROOT / "evals/schemas/weather-repair-report.v1.json").read_text(
            encoding="utf-8"
        )
    )
    committed_report = json.loads(
        (REPOSITORY_ROOT / "evals/reports/weather-repair-fixture.v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert suite_schema == WeatherRepairEvalSuite.model_json_schema(mode="validation")
    assert report_schema == WeatherRepairBaselineReport.model_json_schema(mode="validation")
    assert committed_report == fixture_report.model_dump(mode="json")
