import json
from pathlib import Path

from app.evaluation.comparison_contracts import ComparisonEvalSuite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUITE_SCHEMA_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "comparison-suite.v1.json"


def main() -> None:
    SUITE_SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUITE_SCHEMA_PATH.write_text(
        json.dumps(
            ComparisonEvalSuite.model_json_schema(mode="validation"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
