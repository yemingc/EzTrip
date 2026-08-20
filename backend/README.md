# EzTrip API

Gate 0 FastAPI service. It exposes a liveness-style health endpoint, an empty Alembic baseline, an isolated LangGraph/LangSmith observability probe, versioned V1 travel domain contracts, a deterministic `TripRequest` to `PlannerContext` compiler, and a typed AMap provider with live and fixture transports. The probes, compiler, and provider are not yet part of the product API or a production planning graph.

```powershell
uv sync --all-groups
uv run uvicorn app.main:app --reload
```

Run offline checks:

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy app scripts
```

Regenerate the committed domain JSON Schema bundle after changing a contract:

```powershell
uv run python -m scripts.export_domain_schemas
uv run python -m scripts.export_planner_context_example
```

Compile the committed Beijing request into a deterministic planning context:

```powershell
uv run python -m scripts.export_planner_context_example
uv run pytest tests/test_planner_context.py tests/test_domain_contract_examples.py --no-cov
```

The compiler derives dates, lodging nights, room nights, budget reference amounts, constraint scopes, clarification questions, and capability readiness. It does not parse raw Chinese, call an LLM, fetch candidates, or generate an itinerary.

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

Run the fixed provider adapter scenario offline or live:

```powershell
uv run python -m scripts.run_amap_provider_smoke
uv run python -m scripts.run_amap_provider_smoke --live
```

Both modes return the same `CandidatePOI`, `WeatherRisk`, and `RouteLeg` contracts. The live flag is explicit because it uses the configured AMap Key and network.
