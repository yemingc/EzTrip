import json
from pathlib import Path
from typing import Any

from app.domain import TripRequest
from app.planning import compile_planner_context

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_PATH = REPOSITORY_ROOT / "docs" / "contracts" / "examples" / "trip-request.v1.json"
DEFAULT_OUTPUT_PATH = (
    REPOSITORY_ROOT / "docs" / "contracts" / "examples" / "planner-context.v1.json"
)


def build_planner_context_example(
    input_path: Path = DEFAULT_INPUT_PATH,
) -> dict[str, Any]:
    request = TripRequest.model_validate_json(input_path.read_text(encoding="utf-8"))
    context = compile_planner_context(request)
    return context.model_dump(mode="json")


def write_planner_context_example(
    output_path: Path = DEFAULT_OUTPUT_PATH,
    input_path: Path = DEFAULT_INPUT_PATH,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            build_planner_context_example(input_path),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    write_planner_context_example()
