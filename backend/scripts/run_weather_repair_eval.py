import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from app.evaluation import evaluate_weather_repair_suite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = REPOSITORY_ROOT / "evals" / "reports" / "weather-repair-fixture.v1.json"


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise ValueError("Weather Repair fixture evaluation does not accept arguments")
    report = asyncio.run(evaluate_weather_repair_suite())
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
                "no_false_positive_cases": report.no_false_positive_case_count,
                "proactive_tasks": report.proactive_task_case_count,
                "auto_applied_cases": report.auto_applied_case_count,
                "hitl_cases": report.hitl_case_count,
                "bounded_retry_cases": report.bounded_retry_case_count,
                "source_traceability_rate": str(report.source_traceability_rate),
                "coordinator_model_calls": report.coordinator_model_call_count,
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.passed_case_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
