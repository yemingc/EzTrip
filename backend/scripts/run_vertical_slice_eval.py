import argparse
import asyncio
import json
from pathlib import Path

from langsmith import tracing_context

from app.evaluation import (
    VerticalSliceGateReport,
    evaluate_vertical_slice_suite,
    load_vertical_slice_suite,
    run_vertical_slice_case,
)
from app.evaluation.vertical_slice import (
    VERTICAL_SLICE_NORMAL_RESULT_PATH,
    VERTICAL_SLICE_REPORT_PATH,
)
from app.planning import VerticalSliceOutcome, VerticalSliceResult


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the offline EZ-104 Beijing three-day Gate 2 vertical slice."
    )
    parser.add_argument(
        "--write-report",
        action="store_true",
        help="Regenerate the committed Gate 2 report and normal full-result artifact.",
    )
    return parser.parse_args()


async def _run() -> tuple[VerticalSliceGateReport, VerticalSliceResult]:
    suite = load_vertical_slice_suite()
    report = await evaluate_vertical_slice_suite()
    normal_case = next(
        case for case in suite.cases if case.expected.outcome == VerticalSliceOutcome.READY
    )
    normal_result, _ = await run_vertical_slice_case(normal_case)
    return report, normal_result


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    with tracing_context(enabled=False):
        report, normal_result = asyncio.run(_run())
    if args.write_report:
        _write_json(VERTICAL_SLICE_REPORT_PATH, report.model_dump(mode="json"))
        _write_json(
            VERTICAL_SLICE_NORMAL_RESULT_PATH,
            normal_result.model_dump(mode="json"),
        )
    summary = {
        "suite": report.suite,
        "dataset_sha256": report.dataset_sha256,
        "passed_cases": f"{report.passed_case_count}/{report.case_count}",
        "passed_checks": f"{report.passed_check_count}/{report.check_count}",
        "source_traceability": (f"{report.traceable_candidate_count}/{report.candidate_count}"),
        "deterministic_replays": (f"{report.deterministic_replay_count}/{report.case_count}"),
        "report_written": args.write_report,
        "langsmith_upload": False,
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0 if report.passed_case_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
