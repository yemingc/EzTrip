import json
from pathlib import Path

from app.evaluation import SinglePlannerBaselineReport

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "single-planner-report.v1.json"


def build_single_planner_report_schema() -> dict[str, object]:
    return SinglePlannerBaselineReport.model_json_schema(mode="validation")


def main() -> None:
    DEFAULT_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT_PATH.write_text(
        json.dumps(build_single_planner_report_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
