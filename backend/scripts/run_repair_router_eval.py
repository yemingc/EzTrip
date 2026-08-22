import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from app.evaluation import evaluate_repair_router_suite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = REPOSITORY_ROOT / "evals" / "reports" / "repair-router-fixture.v1.json"


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise ValueError("Repair Router fixture evaluation does not accept arguments")
    report = asyncio.run(evaluate_repair_router_suite())
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
                "exact_route_rate": str(report.exact_route_rate),
                "retry_bound_cases": report.retry_bound_case_count,
                "unaffected_reuse_cases": report.unaffected_reuse_case_count,
                "repair_attempts": report.total_repair_attempt_count,
                "router_model_calls": report.router_model_call_count,
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.passed_case_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
