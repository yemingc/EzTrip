import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from app.agents import run_live_constraint_agent
from app.core.config import get_settings
from app.evaluation import evaluate_constraint_agent_suite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = (
    REPOSITORY_ROOT / "evals" / "reports" / "deepseek-constraint-agent-baseline-2026-08-21.json"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the 10-case Constraint Agent evaluation against live DeepSeek."
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
    report = evaluate_constraint_agent_suite(
        lambda request: run_live_constraint_agent(request, settings),
        execution_mode="live",
        model=settings.deepseek_model,
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
                "semantic_precision": str(report.semantic_precision),
                "semantic_recall": str(report.semantic_recall),
                "confirmation_accuracy": str(report.confirmation_accuracy),
                "clarification_case_rate": str(report.clarification_case_rate),
                "total_tokens": report.total_tokens,
                "p50_latency_ms": report.p50_latency_ms,
                "p95_latency_ms": report.p95_latency_ms,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
