import argparse
import asyncio
import json
from pathlib import Path

from app.evaluation.checkpoint import (
    CHECKPOINT_HITL_SUITE_PATH,
    evaluate_checkpoint_hitl_suite,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay the EZ-201 checkpoint and HITL gate")
    parser.add_argument("--suite", type=Path, default=CHECKPOINT_HITL_SUITE_PATH)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


async def run() -> int:
    args = parse_args()
    report = await evaluate_checkpoint_hitl_suite(args.suite)
    payload = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if report.passed_case_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
