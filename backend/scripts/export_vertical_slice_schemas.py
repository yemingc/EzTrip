import json
from pathlib import Path

from app.evaluation import VerticalSliceGateReport, VerticalSliceSuite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUITE_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "vertical-slice-suite.v1.json"
REPORT_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "vertical-slice-report.v1.json"


def build_vertical_slice_suite_schema() -> dict[str, object]:
    return VerticalSliceSuite.model_json_schema(mode="validation")


def build_vertical_slice_report_schema() -> dict[str, object]:
    return VerticalSliceGateReport.model_json_schema(mode="validation")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    _write_json(SUITE_SCHEMA_PATH, build_vertical_slice_suite_schema())
    _write_json(REPORT_SCHEMA_PATH, build_vertical_slice_report_schema())


if __name__ == "__main__":
    main()
