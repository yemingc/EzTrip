import json
from pathlib import Path

from app.evaluation.contracts import PlanningSeedCase

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "planning-seed-case.v1.json"


def build_planning_seed_schema() -> dict[str, object]:
    schema = PlanningSeedCase.model_json_schema(mode="validation")
    schema["$id"] = "https://eztrip.local/schemas/planning-seed-case.v1.json"
    schema["title"] = "EzTrip executable planning seed case V1"
    return schema


def write_planning_seed_schema(output_path: Path = DEFAULT_OUTPUT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(build_planning_seed_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    write_planning_seed_schema()
