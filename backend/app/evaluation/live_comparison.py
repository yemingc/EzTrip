import hashlib
import json
from pathlib import Path

from pydantic import SecretStr

from app.agents.plan_agent_contracts import PlanAgentRunStatus
from app.core.config import Settings
from app.evaluation.explore import explore_agent_dataset_sha256, load_explore_agent_suite
from app.evaluation.live_comparison_contracts import (
    LiveComparisonPilotSuite,
    LiveComparisonPreflight,
)
from app.evaluation.plan_agent import load_plan_agent_suite
from app.evaluation.planning_materials_contracts import RouteFailureInjection
from app.evaluation.stay import load_stay_agent_suite, stay_agent_dataset_sha256

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LIVE_COMPARISON_PILOT_SUITE_PATH = (
    REPOSITORY_ROOT / "evals" / "cases" / "live-comparison" / "suite.v1.json"
)


class LiveComparisonProtocolError(RuntimeError):
    """Raised when the live pilot contradicts its frozen source inventory."""


def _secret_configured(value: SecretStr | None) -> bool:
    return value is not None and bool(value.get_secret_value().strip())


def _referenced_inventory(suite: LiveComparisonPilotSuite) -> dict[str, object]:
    plan_suite = load_plan_agent_suite()
    explore_suite = load_explore_agent_suite()
    stay_suite = load_stay_agent_suite()
    plan_by_id = {item.case_id: item for item in plan_suite.cases}
    explore_by_id = {item.case_id: item for item in explore_suite.cases}
    stay_by_id = {item.case_id: item for item in stay_suite.cases}
    referenced_plans: dict[str, object] = {}
    referenced_explore: dict[str, object] = {}
    referenced_stays: dict[str, object] = {}
    for case in suite.cases:
        try:
            source = plan_by_id[case.source_plan_case_id]
            explore = explore_by_id[source.explore_fixture_case_id]
            stay = stay_by_id[source.stay_fixture_case_id]
        except KeyError as error:
            raise LiveComparisonProtocolError(
                f"unknown live comparison source reference: {error.args[0]}"
            ) from error
        if (
            source.expected.run_status != PlanAgentRunStatus.PLANNED
            or source.route_failure != RouteFailureInjection.NONE
        ):
            raise LiveComparisonProtocolError(
                f"live comparison source must be a ready Plan Agent case: {source.case_id}"
            )
        referenced_plans[source.case_id] = source.model_dump(mode="json")
        referenced_explore[explore.case_id] = explore.model_dump(mode="json")
        referenced_stays[stay.case_id] = stay.model_dump(mode="json")
    return {
        "plan_cases": referenced_plans,
        "explore_cases": referenced_explore,
        "stay_cases": referenced_stays,
        "explore_dataset_sha256": explore_agent_dataset_sha256(explore_suite),
        "stay_dataset_sha256": stay_agent_dataset_sha256(stay_suite),
    }


def load_live_comparison_pilot_suite(
    suite_path: Path = LIVE_COMPARISON_PILOT_SUITE_PATH,
) -> LiveComparisonPilotSuite:
    suite = LiveComparisonPilotSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))
    _referenced_inventory(suite)
    return suite


def live_comparison_pilot_dataset_sha256(suite: LiveComparisonPilotSuite) -> str:
    canonical = json.dumps(
        {
            "suite": suite.model_dump(mode="json"),
            "references": _referenced_inventory(suite),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_live_comparison_preflight(settings: Settings) -> LiveComparisonPreflight:
    suite = load_live_comparison_pilot_suite()
    deepseek_ready = _secret_configured(settings.deepseek_api_key)
    langsmith_ready = _secret_configured(settings.langsmith_api_key)
    model_matches_suite = settings.deepseek_model == suite.model_name
    reasons: list[str] = []
    if not deepseek_ready:
        reasons.append("deepseek_api_key_missing")
    if not langsmith_ready:
        reasons.append("langsmith_api_key_missing")
    if not settings.langsmith_tracing:
        reasons.append("langsmith_tracing_disabled")
    if not model_matches_suite:
        reasons.append("deepseek_model_mismatch")
    budget = suite.call_budget
    return LiveComparisonPreflight(
        dataset_sha256=live_comparison_pilot_dataset_sha256(suite),
        model=settings.deepseek_model,
        expected_model=suite.model_name,
        model_matches_suite=model_matches_suite,
        case_count=len(suite.cases),
        repetitions_per_case=suite.repetitions_per_case,
        trial_count=budget.trial_count,
        base_model_calls=budget.trial_count * budget.base_model_calls_per_trial,
        repair_model_call_allowance=(
            budget.trial_count * budget.repair_model_call_allowance_per_trial
        ),
        max_model_calls=budget.max_model_calls,
        max_completion_tokens=budget.max_completion_tokens,
        amap_calls_planned=0,
        deepseek_key_configured=deepseek_ready,
        langsmith_key_configured=langsmith_ready,
        langsmith_tracing_enabled=settings.langsmith_tracing,
        ready_for_explicit_live_run=not reasons,
        blocking_reasons=tuple(reasons),
    )


__all__ = [
    "LIVE_COMPARISON_PILOT_SUITE_PATH",
    "LiveComparisonProtocolError",
    "build_live_comparison_preflight",
    "live_comparison_pilot_dataset_sha256",
    "load_live_comparison_pilot_suite",
]
