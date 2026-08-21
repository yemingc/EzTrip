import argparse
import asyncio
import json

from langsmith import tracing_context

from app.evaluation import evaluate_planning_seed_suite
from app.evaluation.planning_seed import PLANNING_SEED_REPORT_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the deterministic EZ-008 planning seed baseline offline."
    )
    parser.add_argument(
        "--write-report",
        action="store_true",
        help="Regenerate the committed deterministic baseline report.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with tracing_context(enabled=False):
        report = asyncio.run(evaluate_planning_seed_suite())
    if args.write_report:
        PLANNING_SEED_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLANNING_SEED_REPORT_PATH.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    summary = {
        "suite": report.suite,
        "workflow_version": report.workflow_version,
        "dataset_sha256": report.dataset_sha256,
        "passed_cases": f"{report.passed_case_count}/{report.case_count}",
        "passed_checks": f"{report.passed_check_count}/{report.check_count}",
        "source_traceability": (f"{report.traceable_candidate_count}/{report.candidate_count}"),
        "report_written": args.write_report,
        "langsmith_upload": False,
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0 if report.passed_case_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
