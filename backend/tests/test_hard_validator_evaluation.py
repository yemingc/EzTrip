import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation import (
    HardValidatorBaselineReport,
    HardValidatorEvalSuite,
    evaluate_hard_validator_suite,
    hard_validator_dataset_sha256,
    load_hard_validator_suite,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def fixture_report() -> HardValidatorBaselineReport:
    return asyncio.run(evaluate_hard_validator_suite())


def test_hard_validator_suite_contract_and_referenced_hash_are_stable() -> None:
    suite = load_hard_validator_suite()

    assert len(suite.cases) == 12
    assert len({item.case_id for item in suite.cases}) == 12
    assert len(hard_validator_dataset_sha256(suite)) == 64


def test_fixture_suite_proves_exact_issue_detection_routing_and_replay(
    fixture_report: HardValidatorBaselineReport,
) -> None:
    assert fixture_report.passed_case_count == fixture_report.case_count == 12
    assert fixture_report.exact_issue_set_case_count == 12
    assert fixture_report.exact_issue_set_rate == 1
    assert fixture_report.routing_match_count == fixture_report.expected_issue_count == 22
    assert fixture_report.routing_accuracy == 1
    assert fixture_report.deterministic_replay_case_count == 12
    assert fixture_report.validator_model_call_count == 0


def test_suite_rejects_status_that_disagrees_with_expected_issue_severity() -> None:
    suite = load_hard_validator_suite()
    payload = suite.model_dump(mode="json")
    payload["cases"][1]["expected"]["status"] = "warning"

    with pytest.raises(ValidationError, match="status and finalization"):
        HardValidatorEvalSuite.model_validate(payload)


def test_report_rejects_drifted_aggregate(
    fixture_report: HardValidatorBaselineReport,
) -> None:
    payload = fixture_report.model_dump(mode="json")
    payload["routing_match_count"] -= 1

    with pytest.raises(ValidationError, match="must match hard-validator"):
        HardValidatorBaselineReport.model_validate(payload)


def test_committed_hard_validator_schemas_and_report_match_code(
    fixture_report: HardValidatorBaselineReport,
) -> None:
    suite_schema = json.loads(
        (REPOSITORY_ROOT / "evals/schemas/hard-validator-suite.v1.json").read_text(encoding="utf-8")
    )
    report_schema = json.loads(
        (REPOSITORY_ROOT / "evals/schemas/hard-validator-report.v1.json").read_text(
            encoding="utf-8"
        )
    )
    committed_report = json.loads(
        (REPOSITORY_ROOT / "evals/reports/hard-validator-fixture.v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert suite_schema == HardValidatorEvalSuite.model_json_schema(mode="validation")
    assert report_schema == HardValidatorBaselineReport.model_json_schema(mode="validation")
    assert committed_report == fixture_report.model_dump(mode="json")
