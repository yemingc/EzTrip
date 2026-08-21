import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from app.evaluation import evaluate_hard_validator_suite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = REPOSITORY_ROOT / "evals" / "reports" / "hard-validator-fixture.v1.json"


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise ValueError("hard-validator fixture evaluation does not accept arguments")
    report = asyncio.run(evaluate_hard_validator_suite())
    DEFAULT_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT_PATH.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report": str(DEFAULT_OUTPUT_PATH),
                "passed_cases": f"{report.passed_case_count}/{report.case_count}",
                "exact_issue_set_rate": str(report.exact_issue_set_rate),
                "routing_accuracy": str(report.routing_accuracy),
                "deterministic_replays": report.deterministic_replay_case_count,
                "validator_model_calls": report.validator_model_call_count,
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.passed_case_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
