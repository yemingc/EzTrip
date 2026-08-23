import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agents.contracts import ModelTokenUsage
from app.evaluation.comparison_contracts import COMPARISON_ARMS
from app.evaluation.live_comparison import load_live_comparison_pilot_suite
from app.evaluation.live_comparison_run_contracts import (
    LiveCallOwner,
    LiveCallPhase,
    LiveComparisonPilotReport,
    LiveComparisonRunJournal,
    LiveExecutionMode,
    LiveModelCallRecord,
    LiveModelCallStatus,
    LiveModelNode,
)
from app.evaluation.live_comparison_runner import (
    FixtureLivePilotModelFactory,
    LiveCallBudgetExceeded,
    LiveCallBudgetGuard,
    LiveJournalWriter,
    evaluate_live_comparison_pilot,
)
from scripts import run_live_system_comparison_pilot as live_script

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def fixture_report(tmp_path_factory: pytest.TempPathFactory) -> LiveComparisonPilotReport:
    journal_path = tmp_path_factory.mktemp("live-comparison") / "journal.json"
    return asyncio.run(
        evaluate_live_comparison_pilot(
            FixtureLivePilotModelFactory(),
            model="deepseek-v4-pro",
            journal_path=journal_path,
        )
    )


def test_fixture_contract_executes_six_paired_trials_without_external_calls(
    fixture_report: LiveComparisonPilotReport,
) -> None:
    assert fixture_report.execution_mode == LiveExecutionMode.FIXTURE_CONTRACT
    assert fixture_report.live_calls_performed is False
    assert fixture_report.langsmith_tracing_enabled is False
    assert fixture_report.trial_count == len(fixture_report.trials) == 6
    assert fixture_report.physical_model_call_count == 42
    assert fixture_report.failed_model_call_count == 0
    assert fixture_report.amap_call_count == fixture_report.external_provider_call_count == 0
    assert tuple(item.arm for item in fixture_report.arms) == COMPARISON_ARMS


def test_product_arms_share_exact_initial_plan_and_physical_calls(
    fixture_report: LiveComparisonPilotReport,
) -> None:
    for trial in fixture_report.trials:
        _, no_gate, full = trial.arms
        assert trial.product_initial_plan_sha256 is not None
        assert no_gate.initial_plan_sha256 == full.initial_plan_sha256
        assert no_gate.initial_plan_sha256 == trial.product_initial_plan_sha256
        assert no_gate.logical_model_call_indices == full.logical_model_call_indices
        assert no_gate.logical_model_call_count == full.logical_model_call_count == 5
        assert trial.external_provider_call_count == 0


def test_journal_is_completed_and_partitions_all_calls(tmp_path: Path) -> None:
    journal_path = tmp_path / "journal.json"
    report = asyncio.run(
        evaluate_live_comparison_pilot(
            FixtureLivePilotModelFactory(),
            model="deepseek-v4-pro",
            journal_path=journal_path,
        )
    )
    journal = LiveComparisonRunJournal.model_validate_json(journal_path.read_text(encoding="utf-8"))

    assert journal.status == "completed"
    assert journal.completed_trial_count == 6
    assert journal.current_trial_id is None
    assert journal.calls == report.calls
    assert journal.trials == report.trials


def test_journal_does_not_regress_terminal_calls_on_stale_parallel_update(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.json"
    started_at = datetime.now(UTC)
    writer = LiveJournalWriter(
        path,
        dataset_sha256="0" * 64,
        model="deepseek-v4-pro",
        started_at=started_at,
    )
    started = LiveModelCallRecord(
        call_index=1,
        trial_id="live-comparison-journal-test-v1-r1",
        case_id="live-comparison-journal-test-v1",
        repetition=1,
        owner=LiveCallOwner.SINGLE_AGENT,
        phase=LiveCallPhase.BASE,
        node=LiveModelNode.SINGLE_SELECTION,
        model="deepseek-v4-pro",
        max_completion_tokens=900,
        status=LiveModelCallStatus.STARTED,
    )
    succeeded = started.model_copy(
        update={
            "status": LiveModelCallStatus.SUCCEEDED,
            "latency_ms": 10,
            "usage": ModelTokenUsage(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            ),
        }
    )

    writer.update_calls((succeeded,))
    writer.update_calls((started,))
    journal = LiveComparisonRunJournal.model_validate_json(path.read_text(encoding="utf-8"))

    assert journal.calls == (succeeded,)


def test_budget_guard_rejects_single_and_repair_overruns_before_call() -> None:
    budget = load_live_comparison_pilot_suite().call_budget
    guard = LiveCallBudgetGuard(budget, "deepseek-v4-pro")
    guard.begin(
        trial_id="live-comparison-budget-test-v1-r1",
        case_id="live-comparison-budget-test-v1",
        repetition=1,
        owner=LiveCallOwner.SINGLE_AGENT,
        phase=LiveCallPhase.BASE,
        node=LiveModelNode.SINGLE_SELECTION,
    )
    guard.begin(
        trial_id="live-comparison-budget-test-v1-r1",
        case_id="live-comparison-budget-test-v1",
        repetition=1,
        owner=LiveCallOwner.SINGLE_AGENT,
        phase=LiveCallPhase.BASE,
        node=LiveModelNode.SINGLE_PLAN,
    )
    with pytest.raises(LiveCallBudgetExceeded, match="Single base"):
        guard.begin(
            trial_id="live-comparison-budget-test-v1-r1",
            case_id="live-comparison-budget-test-v1",
            repetition=1,
            owner=LiveCallOwner.SINGLE_AGENT,
            phase=LiveCallPhase.BASE,
            node=LiveModelNode.SINGLE_PLAN,
        )

    for _ in range(2):
        guard.begin(
            trial_id="live-comparison-budget-test-v1-r1",
            case_id="live-comparison-budget-test-v1",
            repetition=1,
            owner=LiveCallOwner.PRODUCT_REPAIR,
            phase=LiveCallPhase.REPAIR,
            node=LiveModelNode.PRODUCT_PLAN,
            max_completion_tokens=1200,
        )
    with pytest.raises(LiveCallBudgetExceeded, match="repair"):
        guard.begin(
            trial_id="live-comparison-budget-test-v1-r1",
            case_id="live-comparison-budget-test-v1",
            repetition=1,
            owner=LiveCallOwner.PRODUCT_REPAIR,
            phase=LiveCallPhase.REPAIR,
            node=LiveModelNode.PRODUCT_PLAN,
            max_completion_tokens=1200,
        )


def test_cli_requires_two_explicit_live_guards_before_loading_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        live_script,
        "get_settings",
        lambda: pytest.fail("settings must not load before both live guards pass"),
    )

    assert live_script.main([]) == 2
    first = json.loads(capsys.readouterr().out)
    assert first["external_calls_performed"] is False
    assert live_script.main(["--live", "--confirm-max-model-calls", "53"]) == 2
    second = json.loads(capsys.readouterr().out)
    assert second["external_calls_performed"] is False


@pytest.mark.parametrize(
    ("filename", "contract"),
    (
        ("live-comparison-pilot-report.v1.json", LiveComparisonPilotReport),
        ("live-comparison-run-journal.v1.json", LiveComparisonRunJournal),
    ),
)
def test_committed_live_run_schemas_match_contract(
    filename: str,
    contract: type[LiveComparisonPilotReport] | type[LiveComparisonRunJournal],
) -> None:
    schema = json.loads(
        (REPOSITORY_ROOT / "evals" / "schemas" / filename).read_text(encoding="utf-8")
    )

    assert schema == contract.model_json_schema(mode="validation")
