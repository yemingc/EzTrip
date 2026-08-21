import asyncio
import json
from pathlib import Path

from app.evaluation import evaluate_planning_material_suite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_PATH = REPOSITORY_ROOT / "evals" / "reports" / "planning-materials-fixture.v1.json"


def main() -> int:
    report = asyncio.run(evaluate_planning_material_suite())
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report": str(OUTPUT_PATH),
                "passed_cases": f"{report.passed_case_count}/{report.case_count}",
                "route_edges": f"{report.actual_edge_count}/{report.expected_edge_count}",
                "route_provider_calls": report.route_provider_call_count,
                "typed_route_failures": report.typed_route_failure_count,
                "exact_budget_cases": report.exact_budget_case_count,
                "bounded_concurrency_cases": report.bounded_concurrency_case_count,
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.passed_case_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
