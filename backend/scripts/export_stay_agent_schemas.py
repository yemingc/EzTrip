import json
from pathlib import Path

from app.evaluation import StayAgentBaselineReport, StayAgentEvalSuite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "stay-agent-suite.v1.json"
DEFAULT_REPORT_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "stay-agent-report.v1.json"


def build_stay_agent_suite_schema() -> dict[str, object]:
    return StayAgentEvalSuite.model_json_schema(mode="validation")


def build_stay_agent_report_schema() -> dict[str, object]:
    return StayAgentBaselineReport.model_json_schema(mode="validation")


def main() -> None:
    outputs = (
        (DEFAULT_SUITE_SCHEMA_PATH, build_stay_agent_suite_schema()),
        (DEFAULT_REPORT_SCHEMA_PATH, build_stay_agent_report_schema()),
    )
    for path, schema in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
