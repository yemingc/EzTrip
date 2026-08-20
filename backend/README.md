# EzTrip API

Gate 0 FastAPI service. It exposes a liveness-style health endpoint, an empty Alembic baseline, an isolated LangGraph/LangSmith observability probe, versioned V1 travel domain contracts, and an isolated AMap MCP/REST contract probe. Neither probe is part of the product API.

```powershell
uv sync --all-groups
uv run uvicorn app.main:app --reload
```

Run offline checks:

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy app
```

Regenerate the committed domain JSON Schema bundle after changing a contract:

```powershell
uv run python -m scripts.export_domain_schemas
```

Run the live observability probe only after configuring the local root `.env`:

```powershell
uv run python -m scripts.run_observability_probe
uv run python -m scripts.run_observability_probe --force-tool-error
```

The live commands use DeepSeek and LangSmith Cloud. They never call AMap and must not run in CI.

Run the low-volume AMap live contract probe only when intentionally refreshing its sanitized fixture:

```powershell
uv run python -m scripts.run_amap_mcp_probe --live --write-fixture
```

This command uses the root `.env` AMap key. CI only validates the committed allow-listed fixture and never makes live provider calls.
