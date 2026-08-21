import json
from pathlib import Path

from app.domain.base import DomainModel
from app.evaluation import (
    ConstraintAgentBaselineReport,
    ConstraintAgentExpectationSuite,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXPECTATION_SCHEMA_PATH = (
    REPOSITORY_ROOT / "evals" / "schemas" / "constraint-agent-expectations.v1.json"
)
DEFAULT_REPORT_SCHEMA_PATH = (
    REPOSITORY_ROOT / "evals" / "schemas" / "constraint-agent-report.v1.json"
)


def write_schema(model: type[DomainModel], output_path: Path) -> None:
    schema = model.model_json_schema(mode="validation")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    write_schema(ConstraintAgentExpectationSuite, DEFAULT_EXPECTATION_SCHEMA_PATH)
    write_schema(ConstraintAgentBaselineReport, DEFAULT_REPORT_SCHEMA_PATH)


if __name__ == "__main__":
    main()
