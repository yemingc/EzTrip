import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from app.agents import PlanAgentRunResult, run_live_plan_agent, run_plan_agent
from app.core.config import get_settings
from app.evaluation import (
    PlanAgentEvalCase,
    PlanAgentFixtureModel,
    PlanAgentRunner,
    evaluate_plan_agent_suite,
)
from app.planning.material_contracts import PlanningMaterialBundle

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_OUTPUT_PATH = REPOSITORY_ROOT / "evals" / "reports" / "plan-agent-fixture.v1.json"
LIVE_OUTPUT_PATH = (
    REPOSITORY_ROOT / "evals" / "reports" / "deepseek-plan-agent-baseline-2026-08-21.json"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the six-case Plan Agent grounding and material-consumption suite."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use live DeepSeek and LangSmith for the four ready Plan Agent cases.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    runner: PlanAgentRunner
    execution_mode: Literal["fixture", "live"]
    if args.live:

        def live_runner(
            case: PlanAgentEvalCase,
            materials: PlanningMaterialBundle,
        ) -> PlanAgentRunResult:
            return run_live_plan_agent(case.request, materials, settings)

        runner = live_runner
        execution_mode = "live"
        model = settings.deepseek_model
        output = args.output or LIVE_OUTPUT_PATH
    else:
        fixture_model = PlanAgentFixtureModel()

        def fixture_runner(
            case: PlanAgentEvalCase,
            materials: PlanningMaterialBundle,
        ) -> PlanAgentRunResult:
            return run_plan_agent(case.request, materials, fixture_model)

        runner = fixture_runner
        execution_mode = "fixture"
        model = "fixture-plan-agent-model"
        output = args.output or FIXTURE_OUTPUT_PATH
    report = asyncio.run(
        evaluate_plan_agent_suite(
            runner,
            execution_mode=execution_mode,
            model=model,
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report": str(output),
                "execution_mode": report.execution_mode,
                "passed_cases": f"{report.passed_case_count}/{report.case_count}",
                "planned_cases": report.planned_case_count,
                "skipped_cases": report.skipped_case_count,
                "model_calls": report.model_call_count,
                "candidate_grounding_rate": str(report.grounding_rate),
                "route_lineage_rate": str(report.route_lineage_rate),
                "weather_preservation_rate": str(report.weather_preservation_rate),
                "zero_cost_claim_cases": report.zero_cost_claim_case_count,
                "total_tokens": report.total_tokens,
                "p50_latency_ms": report.p50_latency_ms,
                "p95_latency_ms": report.p95_latency_ms,
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.passed_case_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
