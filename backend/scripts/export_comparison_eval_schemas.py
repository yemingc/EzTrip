import json
from pathlib import Path

from app.evaluation.comparison_contracts import ComparisonEvalSuite
from app.evaluation.comparison_run_contracts import (
    ComparisonRunOutput,
    ComparisonToolSnapshot,
    SystemComparisonReport,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUITE_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "comparison-suite.v1.json"
TOOL_SNAPSHOT_SCHEMA_PATH = (
    REPOSITORY_ROOT / "evals" / "schemas" / "comparison-tool-snapshot.v1.json"
)
RUN_OUTPUT_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "comparison-run-output.v1.json"
REPORT_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "comparison-report.v1.json"


def main() -> None:
    schemas = (
        (SUITE_SCHEMA_PATH, ComparisonEvalSuite),
        (TOOL_SNAPSHOT_SCHEMA_PATH, ComparisonToolSnapshot),
        (RUN_OUTPUT_SCHEMA_PATH, ComparisonRunOutput),
        (REPORT_SCHEMA_PATH, SystemComparisonReport),
    )
    for path, contract in schemas:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                contract.model_json_schema(mode="validation"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )


if __name__ == "__main__":
    main()
