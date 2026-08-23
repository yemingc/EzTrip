import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from app.core.config import get_settings
from app.evaluation.live_comparison import build_live_comparison_preflight
from app.evaluation.live_comparison_runner import run_live_comparison_pilot

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = (
    REPOSITORY_ROOT / "evals" / "reports" / "deepseek-live-system-comparison-pilot-2026-08-23.json"
)
DEFAULT_JOURNAL_PATH = (
    REPOSITORY_ROOT / "backend" / "tmp" / "live-system-comparison-pilot-progress.json"
)
REQUIRED_MODEL_CALL_CONFIRMATION = 54


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the frozen six-trial DeepSeek/LangSmith system comparison pilot."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow external DeepSeek and LangSmith calls.",
    )
    parser.add_argument(
        "--confirm-max-model-calls",
        type=int,
        help="Must equal 54 before live calls are permitted.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_PATH)
    return parser.parse_args(argv)


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.live:
        print(
            json.dumps(
                {
                    "status": "live_flag_required",
                    "required_flag": "--live",
                    "max_model_calls": REQUIRED_MODEL_CALL_CONFIRMATION,
                    "external_calls_performed": False,
                },
                ensure_ascii=False,
            )
        )
        return 2
    if args.confirm_max_model_calls != REQUIRED_MODEL_CALL_CONFIRMATION:
        print(
            json.dumps(
                {
                    "status": "call_budget_confirmation_required",
                    "expected": REQUIRED_MODEL_CALL_CONFIRMATION,
                    "received": args.confirm_max_model_calls,
                    "external_calls_performed": False,
                },
                ensure_ascii=False,
            )
        )
        return 2

    settings = get_settings()
    preflight = build_live_comparison_preflight(settings)
    if not preflight.ready_for_explicit_live_run:
        print(
            json.dumps(
                {
                    "status": "preflight_blocked",
                    "blocking_reasons": preflight.blocking_reasons,
                    "external_calls_performed": False,
                },
                ensure_ascii=False,
            )
        )
        return 2

    try:
        report = asyncio.run(run_live_comparison_pilot(settings, journal_path=args.journal))
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "run_failed",
                    "error_type": type(error).__name__,
                    "journal": str(args.journal),
                },
                ensure_ascii=False,
            )
        )
        return 1

    _write_json_atomic(args.output, report.model_dump(mode="json"))
    print(
        json.dumps(
            {
                "status": "completed",
                "report": str(args.output),
                "journal": str(args.journal),
                "dataset_sha256": report.dataset_sha256,
                "physical_model_calls": report.physical_model_call_count,
                "failed_model_calls": report.failed_model_call_count,
                "actual_total_tokens": report.actual_total_tokens,
                "arms": {
                    arm.arm.value: {
                        "execution_succeeded_trials": arm.execution_succeeded_trial_count,
                        "finalizable_trials": arm.finalizable_trial_count,
                        "finalization_rate": str(arm.finalization_rate),
                    }
                    for arm in report.arms
                },
                "amap_calls": report.amap_call_count,
                "live_calls_performed": report.live_calls_performed,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
