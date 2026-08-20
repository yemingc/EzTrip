import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from langsmith import tracing_context

from app.domain import MinimalPlanningResult, TripRequest
from app.domain.sources import DataMode
from app.planning import run_minimal_planning_graph
from app.providers import load_fixture_amap_provider

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUEST_PATH = REPOSITORY_ROOT / "docs" / "contracts" / "examples" / "trip-request.v1.json"
DEFAULT_OUTPUT_PATH = (
    REPOSITORY_ROOT / "docs" / "contracts" / "examples" / "minimal-planning-result.v1.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the EZ-007 minimal planning graph against the offline AMap fixture."
    )
    parser.add_argument(
        "--write-example",
        action="store_true",
        help="Regenerate the committed full result example after a contract change.",
    )
    return parser.parse_args()


async def build_fixture_result(
    request_path: Path = DEFAULT_REQUEST_PATH,
) -> MinimalPlanningResult:
    request = TripRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
    with tracing_context(enabled=False):
        return await run_minimal_planning_graph(
            request,
            load_fixture_amap_provider(),
            data_mode=DataMode.FIXTURE,
        )


def write_result_example(
    result: MinimalPlanningResult,
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_safe_summary(result: MinimalPlanningResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "workflow_version": result.workflow_version,
        "data_mode": result.data_mode,
        "context_id": result.planner_context.context_id,
        "candidate_queries": [
            {
                "query_id": query.query_id,
                "keywords": query.keywords,
                "source_constraint_id": query.source_constraint_id,
            }
            for query in result.candidate_queries
        ],
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "name": candidate.name,
                "provider": candidate.source.provider,
                "provider_id": candidate.source.provider_id,
                "data_mode": candidate.source.data_mode,
            }
            for candidate in result.candidates
        ],
        "provider_failures": [
            failure.model_dump(mode="json") for failure in result.provider_failures
        ],
        "events": [event.model_dump(mode="json") for event in result.events],
        "raw_payload_printed": False,
    }


def main() -> int:
    args = parse_args()
    result = asyncio.run(build_fixture_result())
    if args.write_example:
        write_result_example(result)
    print(json.dumps(build_safe_summary(result), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
