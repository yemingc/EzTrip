import json
from pathlib import Path

from app.agents import PlanAgentRunResult
from app.evaluation import PlanAgentBaselineReport, PlanAgentEvalSuite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUITE_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "plan-agent-suite.v1.json"
REPORT_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "plan-agent-report.v1.json"
RESULT_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "plan-agent-result.v1.json"


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    _write_json(
        SUITE_SCHEMA_PATH,
        PlanAgentEvalSuite.model_json_schema(mode="validation"),
    )
    _write_json(
        REPORT_SCHEMA_PATH,
        PlanAgentBaselineReport.model_json_schema(mode="validation"),
    )
    _write_json(
        RESULT_SCHEMA_PATH,
        PlanAgentRunResult.model_json_schema(mode="validation"),
    )


if __name__ == "__main__":
    main()
