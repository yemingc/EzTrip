import hashlib
import json
from pathlib import Path

from app.evaluation.comparison_contracts import ComparisonEvalSuite
from app.evaluation.plan_agent import load_plan_agent_suite, plan_agent_dataset_sha256
from app.evaluation.plan_agent_contracts import PlanAgentEvalCase

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPARISON_SUITE_PATH = REPOSITORY_ROOT / "evals" / "cases" / "comparison" / "suite.v1.json"


class ComparisonEvaluationError(RuntimeError):
    """Raised when the comparison inventory contradicts its frozen references."""


def _referenced_source_cases(
    suite: ComparisonEvalSuite,
) -> tuple[PlanAgentEvalCase, ...]:
    plan_suite = load_plan_agent_suite()
    source_by_id = {item.case_id: item for item in plan_suite.cases}
    referenced: list[PlanAgentEvalCase] = []
    for case in suite.cases:
        try:
            source = source_by_id[case.source_plan_case_id]
        except KeyError as error:
            raise ComparisonEvaluationError(
                f"unknown comparison source Plan Agent case: {error.args[0]}"
            ) from error
        expected_days = (source.request.end_date - source.request.start_date).days + 1
        if (
            case.dimensions.city != source.request.destination_city
            or case.dimensions.trip_days != expected_days
        ):
            raise ComparisonEvaluationError(
                f"comparison dimensions drift from source request: {case.case_id}"
            )
        referenced.append(source)
    missing_sources = set(source_by_id) - {item.case_id for item in referenced}
    if missing_sources:
        raise ComparisonEvaluationError(
            f"comparison inventory does not cover source cases: {sorted(missing_sources)}"
        )
    return tuple(referenced)


def load_comparison_suite(
    suite_path: Path = COMPARISON_SUITE_PATH,
) -> ComparisonEvalSuite:
    suite = ComparisonEvalSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))
    _referenced_source_cases(suite)
    return suite


def comparison_dataset_sha256(suite: ComparisonEvalSuite) -> str:
    plan_suite = load_plan_agent_suite()
    references = _referenced_source_cases(suite)
    unique_references = {item.case_id: item.model_dump(mode="json") for item in references}
    canonical = json.dumps(
        {
            "suite": suite.model_dump(mode="json"),
            "source_plan_cases": unique_references,
            "source_plan_dataset_sha256": plan_agent_dataset_sha256(plan_suite),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "COMPARISON_SUITE_PATH",
    "ComparisonEvaluationError",
    "comparison_dataset_sha256",
    "load_comparison_suite",
]
