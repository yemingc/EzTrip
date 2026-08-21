import json
from pathlib import Path

from app.evaluation import PlanningMaterialBaselineReport, PlanningMaterialEvalSuite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUITE_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "planning-materials-suite.v1.json"
REPORT_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "planning-materials-report.v1.json"


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    _write_json(
        SUITE_SCHEMA_PATH,
        PlanningMaterialEvalSuite.model_json_schema(mode="validation"),
    )
    _write_json(
        REPORT_SCHEMA_PATH,
        PlanningMaterialBaselineReport.model_json_schema(mode="validation"),
    )


if __name__ == "__main__":
    main()
