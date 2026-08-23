import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from app.evaluation import evaluate_system_comparison_fixture

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = REPOSITORY_ROOT / "evals" / "reports" / "system-comparison-fixture.v1.json"


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise ValueError("system comparison fixture evaluation does not accept arguments")
    report = asyncio.run(evaluate_system_comparison_fixture())
    DEFAULT_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT_PATH.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "report": str(DEFAULT_OUTPUT_PATH),
                "dataset_sha256": report.dataset_sha256,
                "full_expectation_matches": (
                    f"{report.full_expectation_match_count}/{report.case_count}"
                ),
                "arms": {
                    arm.arm.value: {
                        "finalizable": (f"{arm.finalizable_case_count}/{arm.eligible_case_count}"),
                        "finalization_rate": str(arm.finalization_rate),
                        "model_calls": arm.model_call_count,
                        "provider_calls": arm.provider_call_count,
                    }
                    for arm in report.arms
                },
                "live_calls_performed": report.live_calls_performed,
                "model_quality_claim_allowed": report.model_quality_claim_allowed,
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.full_expectation_match_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
