import json
from pathlib import Path

from app.evaluation.checkpoint_contracts import CheckpointHitlReport, CheckpointHitlSuite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUITE_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "checkpoint-hitl-suite.v1.json"
REPORT_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "checkpoint-hitl-report.v1.json"


def build_checkpoint_hitl_suite_schema() -> dict[str, object]:
    return CheckpointHitlSuite.model_json_schema(mode="validation")


def build_checkpoint_hitl_report_schema() -> dict[str, object]:
    return CheckpointHitlReport.model_json_schema(mode="validation")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    _write_json(SUITE_SCHEMA_PATH, build_checkpoint_hitl_suite_schema())
    _write_json(REPORT_SCHEMA_PATH, build_checkpoint_hitl_report_schema())


if __name__ == "__main__":
    main()
