import json
from pathlib import Path
from typing import Any

from app.domain import (
    BudgetConstraint,
    CandidatePOI,
    CandidateStay,
    ConstraintSet,
    CostItem,
    DayPlan,
    Party,
    PlannerContext,
    PlanVersion,
    ProviderFailure,
    RouteLeg,
    TripPlan,
    TripRequest,
    ValidationIssue,
    WeatherRisk,
)
from app.domain.base import DomainModel

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = REPOSITORY_ROOT / "docs" / "contracts" / "domain-contracts.v1.json"

SCHEMA_MODELS: tuple[type[DomainModel], ...] = (
    TripRequest,
    PlannerContext,
    Party,
    BudgetConstraint,
    ConstraintSet,
    CandidatePOI,
    CandidateStay,
    WeatherRisk,
    RouteLeg,
    CostItem,
    DayPlan,
    TripPlan,
    ValidationIssue,
    PlanVersion,
    ProviderFailure,
)


def build_schema_bundle() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://eztrip.local/contracts/domain-contracts.v1.json",
        "title": "EzTrip domain contract bundle",
        "version": "1.0",
        "models": {
            model.__name__: model.model_json_schema(mode="validation") for model in SCHEMA_MODELS
        },
    }


def write_schema_bundle(output_path: Path = DEFAULT_OUTPUT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(build_schema_bundle(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    write_schema_bundle()
