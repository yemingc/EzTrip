import json
from pathlib import Path
from typing import Any

from app.agents import (
    ConstraintAgentResult,
    ConstraintProposalBatch,
    ExploreAgentResult,
    ExploreQueryProposalBatch,
    ExploreSelectionProposalBatch,
    PlannerProposalBatch,
    SinglePlannerAgentResult,
)
from app.domain import (
    BudgetConstraint,
    BudgetValidationSummary,
    CandidatePOI,
    CandidateStay,
    ConstraintSet,
    CostItem,
    DayPlan,
    MinimalPlanningResult,
    OpeningHoursEvidence,
    OpeningHoursEvidenceBundle,
    Party,
    PlannerContext,
    PlanValidationReport,
    PlanVersion,
    ProviderFailure,
    RouteLeg,
    TripPlan,
    TripRequest,
    ValidationIssue,
    WeatherRisk,
)
from app.domain.base import DomainModel
from app.planning import (
    HumanReviewRequest,
    HumanReviewResume,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = REPOSITORY_ROOT / "docs" / "contracts" / "domain-contracts.v1.json"

SCHEMA_MODELS: tuple[type[DomainModel], ...] = (
    TripRequest,
    PlannerContext,
    MinimalPlanningResult,
    OpeningHoursEvidence,
    OpeningHoursEvidenceBundle,
    ConstraintProposalBatch,
    ConstraintAgentResult,
    ExploreQueryProposalBatch,
    ExploreSelectionProposalBatch,
    ExploreAgentResult,
    PlannerProposalBatch,
    SinglePlannerAgentResult,
    BudgetValidationSummary,
    PlanValidationReport,
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
    HumanReviewRequest,
    HumanReviewResume,
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
