import json
from pathlib import Path
from typing import Any

from app.tasks.contracts import (
    PlanningTaskAccepted,
    PlanningTaskCreateRequest,
    PlanningTaskEvent,
    PlanningTaskFailure,
    PlanningTaskSnapshot,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_PATH = REPOSITORY_ROOT / "evals" / "schemas" / "planning-task-api.v1.json"
SCHEMA_MODELS = (
    PlanningTaskCreateRequest,
    PlanningTaskAccepted,
    PlanningTaskEvent,
    PlanningTaskSnapshot,
    PlanningTaskFailure,
)


def build_planning_task_schema_bundle() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://eztrip.local/contracts/planning-task-api.v1.json",
        "title": "EzTrip planning task API contract bundle",
        "version": "1.0",
        "models": {
            model.__name__: model.model_json_schema(mode="validation") for model in SCHEMA_MODELS
        },
    }


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(build_planning_task_schema_bundle(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
