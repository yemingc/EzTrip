import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation.repair_router import (
    evaluate_repair_router_suite,
    load_repair_router_suite,
    repair_router_dataset_sha256,
)
from app.evaluation.repair_router_contracts import (
    RepairRouterBaselineReport,
    RepairRouterEvalSuite,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def fixture_report() -> RepairRouterBaselineReport:
    return asyncio.run(evaluate_repair_router_suite())


def test_repair_router_suite_contract_and_hash_are_stable() -> None:
    suite = load_repair_router_suite()

    assert len(suite.cases) == 9
    assert len({item.case_id for item in suite.cases}) == 9
    assert len(repair_router_dataset_sha256(suite)) == 64


def test_fixture_suite_proves_routing_retry_reuse_hitl_and_replay(
    fixture_report: RepairRouterBaselineReport,
) -> None:
    assert fixture_report.passed_case_count == fixture_report.case_count == 9
    assert fixture_report.exact_route_case_count == 9
    assert fixture_report.exact_route_rate == 1
    assert fixture_report.retry_bound_case_count == 9
    assert fixture_report.unaffected_reuse_case_count == 9
    assert fixture_report.deterministic_replay_case_count == 9
    assert fixture_report.total_repair_attempt_count == 9
    assert fixture_report.router_model_call_count == 0


def test_suite_rejects_action_and_node_trace_length_mismatch() -> None:
    suite = load_repair_router_suite()
    payload = suite.model_dump(mode="json")
    payload["cases"][0]["expected"]["executed_nodes_by_attempt"] = []

    with pytest.raises(ValidationError, match="equal lengths"):
        RepairRouterEvalSuite.model_validate(payload)


def test_report_rejects_drifted_aggregate(
    fixture_report: RepairRouterBaselineReport,
) -> None:
    payload = fixture_report.model_dump(mode="json")
    payload["exact_route_case_count"] -= 1

    with pytest.raises(ValidationError, match="must match repair-router"):
        RepairRouterBaselineReport.model_validate(payload)


def test_committed_repair_router_schemas_and_report_match_code(
    fixture_report: RepairRouterBaselineReport,
) -> None:
    suite_schema = json.loads(
        (REPOSITORY_ROOT / "evals/schemas/repair-router-suite.v1.json").read_text(encoding="utf-8")
    )
    report_schema = json.loads(
        (REPOSITORY_ROOT / "evals/schemas/repair-router-report.v1.json").read_text(encoding="utf-8")
    )
    committed_report = json.loads(
        (REPOSITORY_ROOT / "evals/reports/repair-router-fixture.v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert suite_schema == RepairRouterEvalSuite.model_json_schema(mode="validation")
    assert report_schema == RepairRouterBaselineReport.model_json_schema(mode="validation")
    assert committed_report == fixture_report.model_dump(mode="json")
