import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation import (
    COMPARISON_ARMS,
    ComparisonEvalSuite,
    ComparisonEvaluationError,
    ComparisonOutcome,
    ComparisonScenario,
    SeedTier,
    comparison_dataset_sha256,
    load_comparison_suite,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_comparison_suite_freezes_fair_30_case_inventory() -> None:
    suite = load_comparison_suite()

    assert suite.arms == COMPARISON_ARMS
    assert len(suite.cases) == 30
    assert sum(item.tier == SeedTier.STANDARD for item in suite.cases) == 20
    assert sum(item.tier == SeedTier.HARD for item in suite.cases) == 10
    assert {item.scenario for item in suite.cases} == set(ComparisonScenario)
    assert {
        outcome: sum(item.expected.full_outcome == outcome for item in suite.cases)
        for outcome in ComparisonOutcome
    } == {
        ComparisonOutcome.FINALIZABLE_WITHOUT_REPAIR: 5,
        ComparisonOutcome.REPAIRED: 16,
        ComparisonOutcome.WAITING_FOR_USER: 1,
        ComparisonOutcome.UNRESOLVED: 7,
        ComparisonOutcome.BLOCKED_BEFORE_PLAN: 1,
    }
    assert len(comparison_dataset_sha256(suite)) == 64


def test_comparison_hash_covers_suite_content_and_referenced_provider_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = load_comparison_suite()
    original_hash = comparison_dataset_sha256(suite)
    payload = suite.model_dump(mode="json")
    payload["cases"][0]["title"] = "修改后标题"
    changed = ComparisonEvalSuite.model_validate(payload)

    assert comparison_dataset_sha256(changed) != original_hash

    monkeypatch.setattr(
        "app.evaluation.comparison.plan_agent_dataset_sha256",
        lambda _suite: "downstream-provider-fixtures-changed",
    )

    assert comparison_dataset_sha256(suite) != original_hash


def test_comparison_contract_rejects_unfair_arm_inputs() -> None:
    payload = load_comparison_suite().model_dump(mode="json")
    payload["fairness"]["same_post_run_evaluator"] = False

    with pytest.raises(ValidationError):
        ComparisonEvalSuite.model_validate(payload)


def test_comparison_contract_rejects_inconsistent_repaired_outcome() -> None:
    payload = load_comparison_suite().model_dump(mode="json")
    repaired = next(
        item for item in payload["cases"] if item["expected"]["full_outcome"] == "repaired"
    )
    repaired["expected"]["repair_actions"] = []

    with pytest.raises(ValidationError, match="repaired comparison outcomes"):
        ComparisonEvalSuite.model_validate(payload)


def test_loader_rejects_dimensions_that_drift_from_source_request(tmp_path: Path) -> None:
    payload = load_comparison_suite().model_dump(mode="json")
    payload["cases"][0]["dimensions"]["city"] = "天津市"
    suite_path = tmp_path / "drifted-comparison-suite.json"
    suite_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ComparisonEvaluationError, match="dimensions drift"):
        load_comparison_suite(suite_path)


def test_committed_comparison_schema_matches_contract() -> None:
    schema = json.loads(
        (REPOSITORY_ROOT / "evals/schemas/comparison-suite.v1.json").read_text(encoding="utf-8")
    )

    assert schema == ComparisonEvalSuite.model_json_schema(mode="validation")
