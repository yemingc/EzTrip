import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from app.agents import run_live_explore_agent
from app.core.config import get_settings
from app.evaluation import evaluate_explore_agent_suite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = (
    REPOSITORY_ROOT / "evals" / "reports" / "deepseek-explore-agent-baseline-2026-08-21.json"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the six-case Explore Agent evaluation against live DeepSeek."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement that this command performs paid network model calls.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.live:
        raise SystemExit("Pass --live to acknowledge network calls and model usage.")
    settings = get_settings()
    report = asyncio.run(
        evaluate_explore_agent_suite(
            lambda context, provider: run_live_explore_agent(
                context,
                provider,
                settings,
            ),
            execution_mode="live",
            model=settings.deepseek_model,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report": str(args.output),
                "model": report.model,
                "passed_cases": f"{report.passed_case_count}/{report.case_count}",
                "model_calls": report.model_call_count,
                "query_kind_coverage_rate": str(report.query_kind_coverage_rate),
                "grounding_rate": str(report.grounding_rate),
                "source_traceability_rate": str(report.source_traceability_rate),
                "labelled_relevance_rate": str(report.labelled_relevance_rate),
                "recommendation_group_coverage_rate": str(
                    report.recommendation_group_coverage_rate
                ),
                "total_tokens": report.total_tokens,
                "p50_case_latency_ms": report.p50_case_latency_ms,
                "p95_case_latency_ms": report.p95_case_latency_ms,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
