import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from app.core.config import get_settings
from app.domain.request import TripRequest
from app.domain.sources import DataMode
from app.evaluation import (
    FixtureExploreModel,
    FixtureStayModel,
    SpecialistFanoutRunner,
    SpecialistScenarioProvider,
    evaluate_specialist_fanout_suite,
)
from app.planning import (
    SpecialistFanoutResult,
    run_live_specialist_fanout,
    run_specialist_fanout,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_OUTPUT_PATH = REPOSITORY_ROOT / "evals" / "reports" / "specialist-fanout-fixture.v1.json"
LIVE_OUTPUT_PATH = (
    REPOSITORY_ROOT / "evals" / "reports" / "deepseek-specialist-fanout-baseline-2026-08-21.json"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the five-case specialist fan-out orchestration regression suite."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use live DeepSeek models and LangSmith tracing against fixture Providers.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    output = args.output or (LIVE_OUTPUT_PATH if args.live else FIXTURE_OUTPUT_PATH)
    runner: SpecialistFanoutRunner
    execution_mode: Literal["fixture", "live"]
    if args.live:

        async def live_runner(
            request: TripRequest,
            provider: SpecialistScenarioProvider,
        ) -> SpecialistFanoutResult:
            return await run_live_specialist_fanout(
                request,
                provider,
                settings,
                data_mode=DataMode.FIXTURE,
            )

        runner = live_runner
        execution_mode = "live"
        explore_model = settings.deepseek_model
        stay_model = settings.deepseek_model
    else:
        fixture_explore_model = FixtureExploreModel()
        fixture_stay_model = FixtureStayModel()

        async def fixture_runner(
            request: TripRequest,
            provider: SpecialistScenarioProvider,
        ) -> SpecialistFanoutResult:
            return await run_specialist_fanout(
                request,
                provider,
                fixture_explore_model,
                fixture_stay_model,
                data_mode=DataMode.FIXTURE,
            )

        runner = fixture_runner
        execution_mode = "fixture"
        explore_model = "fixture-explore-fanout-model"
        stay_model = "fixture-stay-fanout-model"
    report = asyncio.run(
        evaluate_specialist_fanout_suite(
            runner,
            execution_mode=execution_mode,
            explore_model=explore_model,
            stay_model=stay_model,
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
                "branch_statuses": (
                    f"{report.branch_status_match_count}/{report.branch_expectation_count}"
                ),
                "typed_provider_failures": report.typed_provider_failure_count,
                "preserved_successes": report.preserved_success_count,
                "proactive_weather_calls": report.proactive_weather_call_count,
                "parallel_entry_cases": report.parallel_provider_entry_case_count,
                "model_calls": report.model_call_count,
                "provider_calls": report.provider_call_count,
                "total_tokens": report.total_tokens,
                "p50_fanout_latency_ms": report.p50_fanout_latency_ms,
                "p95_fanout_latency_ms": report.p95_fanout_latency_ms,
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.passed_case_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
