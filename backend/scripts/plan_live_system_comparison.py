import json
from collections.abc import Sequence

from app.core.config import get_settings
from app.evaluation import build_live_comparison_preflight


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise ValueError("live comparison preflight does not accept arguments")
    preflight = build_live_comparison_preflight(get_settings())
    print(json.dumps(preflight.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if preflight.ready_for_explicit_live_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
