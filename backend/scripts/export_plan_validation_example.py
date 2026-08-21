import json
from pathlib import Path
from typing import Any

from app.domain import PlanValidationReport, TripPlan, TripRequest
from app.planning import validate_trip_plan

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUEST_PATH = REPOSITORY_ROOT / "docs" / "contracts" / "examples" / "trip-request.v1.json"
DEFAULT_PLAN_PATH = REPOSITORY_ROOT / "docs" / "contracts" / "examples" / "trip-plan.v1.json"
DEFAULT_OUTPUT_PATH = (
    REPOSITORY_ROOT / "docs" / "contracts" / "examples" / "plan-validation-report.v1.json"
)


def build_plan_validation_example(
    request_path: Path = DEFAULT_REQUEST_PATH,
    plan_path: Path = DEFAULT_PLAN_PATH,
) -> PlanValidationReport:
    request = TripRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
    plan = TripPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    return validate_trip_plan(request, plan)


def write_plan_validation_example(
    output_path: Path = DEFAULT_OUTPUT_PATH,
    request_path: Path = DEFAULT_REQUEST_PATH,
    plan_path: Path = DEFAULT_PLAN_PATH,
) -> None:
    report = build_plan_validation_example(request_path, plan_path)
    payload: dict[str, Any] = report.model_dump(mode="json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    write_plan_validation_example()
