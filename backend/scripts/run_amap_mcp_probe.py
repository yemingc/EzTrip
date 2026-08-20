import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.observability.redaction import TraceRedactor
from app.providers.amap_probe import AmapProbeError, run_live_amap_probe, write_probe_capture

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_PATH = (
    REPOSITORY_ROOT / "evals" / "fixtures" / "amap" / "mcp-beijing-2026-08-20.v1.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the fixed, low-volume AMap live probe.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement that the command makes live AMap calls.",
    )
    parser.add_argument(
        "--write-fixture",
        action="store_true",
        help="Write the allow-listed, redacted capture to the versioned fixture path.",
    )
    return parser.parse_args()


def safe_summary(capture: Any, fixture_path: Path | None) -> dict[str, Any]:
    return {
        "status": "ok",
        "capture_id": capture.capture_id,
        "server": f"{capture.server_name}/{capture.server_version}",
        "protocol_version": capture.protocol_version,
        "mcp_sdk_version": capture.mcp_sdk_version,
        "tool_count": len(capture.tool_catalog),
        "call_count": len(capture.calls),
        "calls": [
            {
                "operation": call.operation,
                "transport": call.transport,
                "latency_ms": call.latency_ms,
            }
            for call in capture.calls
        ],
        "fixture": str(fixture_path.relative_to(REPOSITORY_ROOT)) if fixture_path else None,
        "raw_payload_printed": False,
    }


def main() -> int:
    args = parse_args()
    if not args.live:
        print("Refusing to call AMap without the explicit --live flag.")
        return 2

    settings = Settings()
    redactor = TraceRedactor.from_settings(settings)
    try:
        capture = asyncio.run(run_live_amap_probe(settings))
        fixture_path = DEFAULT_FIXTURE_PATH if args.write_fixture else None
        if fixture_path is not None:
            write_probe_capture(capture, fixture_path, redactor=redactor)
        print(json.dumps(safe_summary(capture, fixture_path), ensure_ascii=True, indent=2))
        return 0
    except AmapProbeError as exc:
        failure = exc.failure.model_dump(mode="json")
        print(json.dumps({"status": "error", "failure": failure}, ensure_ascii=True))
        return 1
    except Exception as exc:
        message = redactor.redact_text(str(exc))
        print(
            json.dumps(
                {"status": "error", "error_type": type(exc).__name__, "message": message},
                ensure_ascii=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
