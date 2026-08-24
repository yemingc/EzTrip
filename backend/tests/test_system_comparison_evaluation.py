import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.base import DomainModel
from app.evaluation import (
    COMPARISON_ARMS,
    ComparisonOutcome,
    ComparisonRunOutput,
    ComparisonToolSnapshot,
    SystemComparisonReport,
    evaluate_system_comparison_fixture,
)
from app.evaluation.comparison_contracts import ComparisonEvalSuite
from app.evaluation.comparison_runner import (
    ComparisonFixtureSingleAgentPolicy,
    build_comparison_tool_snapshot,
)
from app.evaluation.plan_agent import build_plan_agent_materials, load_plan_agent_suite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def fixture_report() -> SystemComparisonReport:
    return asyncio.run(evaluate_system_comparison_fixture())


def test_fixture_comparison_replays_all_three_arms_with_frozen_outcomes(
    fixture_report: SystemComparisonReport,
) -> None:
    assert tuple(item.arm for item in fixture_report.arms) == COMPARISON_ARMS
    assert fixture_report.full_expectation_match_count == fixture_report.case_count == 30
    assert sum(item.protocol_passed_case_count for item in fixture_report.arms) == 90

    single, no_gate, full = fixture_report.arms
    assert (
        single.eligible_case_count,
        no_gate.eligible_case_count,
        full.eligible_case_count,
    ) == (29, 29, 29)
    assert (
        single.finalizable_case_count,
        no_gate.finalizable_case_count,
        full.finalizable_case_count,
    ) == (5, 5, 21)
    assert (
        single.finalization_rate,
        no_gate.finalization_rate,
        full.finalization_rate,
    ) == (Decimal("0.1724"), Decimal("0.1724"), Decimal("0.7241"))
    assert (
        full.finalizable_without_repair_case_count,
        full.repaired_case_count,
        full.waiting_for_user_case_count,
        full.unresolved_case_count,
        full.blocked_case_count,
    ) == (5, 16, 1, 7, 1)


def test_fixture_comparison_is_paired_and_does_not_invent_specialist_lift(
    fixture_report: SystemComparisonReport,
) -> None:
    single, no_gate, full = fixture_report.arms
    assert tuple(item.plan_sha256 for item in single.results) == tuple(
        item.plan_sha256 for item in no_gate.results
    )
    for paired in zip(single.results, no_gate.results, full.results, strict=True):
        assert len({item.tool_snapshot_sha256 for item in paired}) == 1
        assert len({item.fault_fixture_sha256 for item in paired}) == 1
        assert len({item.selected_stay_candidate_id for item in paired}) == 1

    single_to_no_gate, no_gate_to_full, single_to_full = fixture_report.paired_deltas
    assert (
        single_to_no_gate.improved_case_count,
        single_to_no_gate.worsened_case_count,
        single_to_no_gate.unchanged_case_count,
        single_to_no_gate.finalization_rate_delta,
    ) == (0, 0, 29, Decimal("0.0000"))
    for delta in (no_gate_to_full, single_to_full):
        assert (
            delta.improved_case_count,
            delta.worsened_case_count,
            delta.unchanged_case_count,
            delta.finalization_rate_delta,
        ) == (16, 0, 13, Decimal("0.5517"))


def test_single_agent_receives_all_stays_and_selects_its_own_route_anchor() -> None:
    source_case = load_plan_agent_suite().cases[0]
    materials = asyncio.run(build_plan_agent_materials(source_case))
    snapshot = build_comparison_tool_snapshot(materials)

    assert len(snapshot.stay_candidates) == 3
    selected = ComparisonFixtureSingleAgentPolicy().select_stay(snapshot)
    assert selected in {item.candidate_id for item in snapshot.stay_candidates}
    assert selected == snapshot.route_anchor_candidate_id


def test_fixture_report_preserves_cost_and_claim_boundaries(
    fixture_report: SystemComparisonReport,
) -> None:
    single, no_gate, full = fixture_report.arms
    assert fixture_report.live_calls_performed is False
    assert fixture_report.control_path_claim_allowed is True
    assert fixture_report.model_quality_claim_allowed is False
    assert (single.model_call_count, no_gate.model_call_count, full.model_call_count) == (
        29,
        145,
        187,
    )
    assert (single.provider_call_count, no_gate.provider_call_count, full.provider_call_count) == (
        357,
        357,
        518,
    )
    assert single.total_tokens == 6960
    assert no_gate.total_tokens is None and full.total_tokens is None
    assert all(item.p50_latency_ms is None for item in fixture_report.arms)


def test_only_full_product_arm_executes_repair(
    fixture_report: SystemComparisonReport,
) -> None:
    single, no_gate, full = fixture_report.arms
    assert all(not item.repair_actions for item in single.results)
    assert all(not item.repair_actions for item in no_gate.results)
    assert sum(item.repair_stop_reason is not None for item in full.results) == 24
    assert sum(bool(item.repair_actions) for item in full.results) == 21
    assert sum(item.outcome == ComparisonOutcome.REPAIRED for item in full.results) == 16
    assert (
        next(
            item
            for item in full.results
            if item.case_id == "comparison-hard-beijing-budget-floor-hitl-v1"
        ).outcome
        == ComparisonOutcome.WAITING_FOR_USER
    )


def test_report_contract_rejects_paired_hash_or_delta_drift(
    fixture_report: SystemComparisonReport,
) -> None:
    payload = fixture_report.model_dump(mode="json")
    payload["arms"][1]["results"][0]["tool_snapshot_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="share input fixture hashes"):
        SystemComparisonReport.model_validate(payload)

    payload = fixture_report.model_dump(mode="json")
    payload["paired_deltas"][1]["improved_case_count"] = 15
    payload["paired_deltas"][1]["unchanged_case_count"] = 14
    with pytest.raises(ValidationError, match="paired delta must match"):
        SystemComparisonReport.model_validate(payload)


def test_committed_comparison_report_matches_deterministic_replay(
    fixture_report: SystemComparisonReport,
) -> None:
    committed = json.loads(
        (REPOSITORY_ROOT / "evals/reports/system-comparison-fixture.v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert committed == fixture_report.model_dump(mode="json")


@pytest.mark.parametrize(
    ("path", "contract"),
    (
        ("comparison-suite.v1.json", ComparisonEvalSuite),
        ("comparison-tool-snapshot.v1.json", ComparisonToolSnapshot),
        ("comparison-run-output.v1.json", ComparisonRunOutput),
        ("comparison-report.v1.json", SystemComparisonReport),
    ),
)
def test_committed_comparison_schemas_match_contract(
    path: str,
    contract: type[DomainModel],
) -> None:
    schema = json.loads((REPOSITORY_ROOT / "evals/schemas" / path).read_text(encoding="utf-8"))

    assert schema == contract.model_json_schema(mode="validation")
