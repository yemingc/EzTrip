import argparse
import json
from uuid import uuid4

from app.core.config import get_settings
from app.observability.probe import run_live_probe
from app.observability.redaction import TraceRedactor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the isolated EzTrip DeepSeek/LangSmith observability probe."
    )
    parser.add_argument(
        "--force-tool-error",
        action="store_true",
        help="Raise a controlled fixture error so LangSmith records a failed tool span.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    redactor = TraceRedactor.from_settings(settings)
    trace_id = uuid4()

    try:
        result = run_live_probe(
            settings,
            force_tool_error=args.force_tool_error,
            run_id=trace_id,
        )
    except Exception as error:
        payload = {
            "status": "expected_error" if args.force_tool_error else "error",
            "error_type": type(error).__name__,
            "message": redactor.redact_text(str(error)),
            "project": settings.langsmith_project,
            "trace_id": str(trace_id),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if args.force_tool_error else 1

    payload = {
        "status": "success",
        "data_mode": "fixture",
        "model": result["model"],
        "project": result["project"],
        "trace_id": result["trace_id"],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
