# EzTrip API

Gate 0 FastAPI service plus the first offline Gate 2 vertical slice, recoverable HITL wrapper, and specialist fan-out. It includes a liveness-style health endpoint, an empty Alembic baseline, an isolated LangGraph/LangSmith observability probe, versioned V1 travel contracts, a deterministic `TripRequest` to `PlannerContext` compiler, a typed AMap provider, schema-constrained Constraint/Explore/Stay Agents, a provider-grounded single-Planner baseline, a deterministic plan/budget validator, a fixture-backed complete Beijing three-day `TripPlan`, a SQLite-checkpointed main Graph using native LangGraph interrupt/resume, and an independent parallel Explore/Stay/proactive-Weather information-gathering Graph. These components are not yet exposed as a product planning API and the specialist bundle is not yet a final itinerary.

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
uv run python -m scripts.export_constraint_agent_schemas
uv run python -m scripts.export_single_planner_schema
uv run python -m scripts.export_explore_agent_schemas
uv run python -m scripts.export_stay_agent_schemas
uv run python -m scripts.export_checkpoint_hitl_schemas
uv run python -m scripts.export_specialist_fanout_schemas
uv run python -m scripts.export_plan_validation_example
uv run python -m scripts.export_planner_context_example
uv run python -m scripts.run_minimal_planning_graph --write-example
```

Compile the committed Beijing request into a deterministic planning context:

```powershell
uv run python -m scripts.export_planner_context_example
uv run pytest tests/test_planner_context.py tests/test_domain_contract_examples.py --no-cov
```

The compiler derives dates, lodging nights, room nights, budget reference amounts, constraint scopes, clarification questions, and capability readiness. It does not parse raw Chinese, call an LLM, fetch candidates, or generate an itinerary.

Run the first executable planning Graph entirely offline:

```powershell
uv run python -m scripts.run_minimal_planning_graph
uv run pytest tests/test_minimal_planning_graph.py --no-cov
```

The Graph compiles context, applies a capability-specific clarification gate, and searches fixture-backed POIs only for confirmed `must_visit` constraints. It records typed failures and source-traceable candidates. It does not perform open-ended model recommendation, ranking, weather/route joins, budgeting, or itinerary generation.

Run the 6-standard / 4-hard deterministic planning baseline:

```powershell
uv run python -m scripts.export_planning_seed_schema
uv run python -m scripts.run_planning_seed_eval
uv run python -m scripts.run_planning_seed_eval --write-report
uv run pytest tests/test_planning_seed_eval.py --no-cov
```

The committed report currently records 10/10 cases, 120/120 deterministic checks, and 6/6 source-traceable candidates. These are workflow-contract metrics over structured requests and labelled fixtures, not itinerary accuracy or Agent quality.

Run the Constraint Agent tests offline or explicitly refresh its live baseline:

```powershell
uv run pytest tests/test_constraint_agent.py tests/test_constraint_agent_evaluation.py --no-cov
uv run python -m scripts.run_constraint_agent_eval --live
```

The fake-model tests enforce exact evidence, schema output, deterministic IDs, source/confirmation mapping, uncertainty guards, and aggregate report contracts. The live command uses DeepSeek and LangSmith and currently records 9/10 exact cases with one retained hard/soft accessibility ambiguity. It must not run in CI.

Run the single-Planner tests offline or explicitly refresh its live baseline:

```powershell
uv run pytest tests/test_single_planner.py tests/test_single_planner_evaluation.py --no-cov
uv run python -m scripts.run_single_planner_eval --live
```

The Planner can place only the candidate IDs returned by the upstream fixture provider. Deterministic code enforces exact candidate coverage, copies names and sources, validates trip dates and non-overlapping timelines, and assembles partial `DayPlan` objects. The live baseline invokes DeepSeek for 6 eligible cases and stops before the model for 4 ineligible cases. It does not measure itinerary quality and does not generate a complete `TripPlan`.

Run the Explore Agent tests offline or explicitly refresh its live development-set baseline:

```powershell
uv run pytest tests/test_explore_agent.py tests/test_explore_agent_evaluation.py --no-cov
uv run python -m scripts.run_explore_agent_eval --live
```

The four-node subgraph asks the model for attraction/dining search strategies, calls only the injected POI provider, then asks the model to rank provider candidate IDs with typed evidence. Deterministic code owns IDs, facts, source lineage, deduplication and evidence validation. The six-case live report is a prompt-development regression result over fixture catalogs, not a real-time recommendation-accuracy or holdout score, and the subgraph is not yet connected to the stateful main orchestration.

Run the Stay Agent tests offline or explicitly refresh its live development-set baseline:

```powershell
uv run pytest tests/test_stay_agent.py tests/test_stay_agent_evaluation.py --no-cov
uv run python -m scripts.run_stay_agent_eval --live
```

The four-node subgraph asks the model for accommodation-area search strategies, calls only the injected `StaySearchProvider`, then ranks Provider candidate IDs with typed evidence. Code owns candidate facts and rejects blocked contexts before model/Provider calls. The typed AMap adapter converts only hotel-classified POIs and leaves price empty, availability unknown and booking disabled. The six-case fixture-catalog report is a development regression result, not evidence of real-time hotel price, availability or recommendation quality; the subgraph is not yet connected to the stateful main orchestration.

Run the deterministic plan validator and regenerate its committed example:

```powershell
uv run pytest tests/test_plan_validator.py --no-cov
uv run python -m scripts.export_plan_validation_example
```

The validator cross-checks request/plan identity, city and dates, duplicate candidates, recommendation source modes, and budget scope. Budget totals are recomputed from `Decimal` `CostItem` values; missing included categories remain incomplete instead of becoming zero-cost assumptions. Hard errors block finalization through typed `ValidationIssue` results. Route feasibility, must/avoid rule coverage, repair routing, and a product-ready itinerary remain later work.

Run the complete offline Beijing three-day Gate 2 vertical slice:

```powershell
uv run python -m scripts.export_vertical_slice_schemas
uv run python -m scripts.run_vertical_slice_eval
uv run pytest tests/test_vertical_slice.py --no-cov
```

The committed result connects the existing context compiler, fixture provider, Single Planner, deterministic `TripPlan` assembler, and validator. It records 2/2 cases, 20/20 checks, 6/6 traceable candidate occurrences, and 2/2 exact replays. The normal case recomputes a 500 CNY fixture total; the hard case reports a 600 CNY gap without removing candidates or auto-finalizing. These are workflow-contract results over labelled fixtures and a fixed Planner proposal, not live price or model-quality claims.

Run the recoverable checkpoint and HITL gate:

```powershell
uv run python -m scripts.export_checkpoint_hitl_schemas
uv run python -m scripts.run_checkpoint_hitl_eval
uv run pytest tests/test_stateful_planning.py --no-cov
```

The main Graph persists JSON-compatible state in SQLite, pauses with LangGraph `interrupt()`, and resumes from a newly constructed runtime via `Command(resume=...)`. The committed fixture report records 2/2 cases and 20/20 checks; both restored runs make zero provider and Planner-model calls. Approval still leaves `TripPlan.status=draft`, while a conflicted plan cannot use the approval action. SQLite is local evidence, not a production concurrency, encryption, or availability claim.

Run the specialist fan-out regression offline or explicitly refresh its live-model baseline:

```powershell
uv run python -m scripts.export_specialist_fanout_schemas
uv run python -m scripts.run_specialist_fanout_eval
uv run python -m scripts.run_specialist_fanout_eval --live
uv run pytest tests/test_specialist_fanout.py tests/test_specialist_fanout_eval.py --no-cov
```

The Graph fans out to Explore, Stay, and zero-model Weather branches, then merges one reducer-accumulated result per specialist. Capability blocks skip only the affected branch; typed Provider failures preserve the other branches and retain completed model usage for cost accounting. The live-model report uses DeepSeek and LangSmith over fixture Providers. It proves orchestration mechanics, not real-time AMap quality or a final multi-Agent itinerary improvement.

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
