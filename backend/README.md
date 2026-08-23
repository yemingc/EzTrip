# EzTrip API

Gate 0 FastAPI service plus the first offline Gate 2 vertical slice, recoverable HITL wrapper, specialist fan-out, deterministic planning-material layer, schema-constrained multi-Agent Plan Agent, deterministic Hard Validators, a bounded Repair Router, and provider-triggered local Weather Repair. It includes a liveness-style health endpoint, an empty Alembic baseline, an isolated LangGraph/LangSmith observability probe, versioned V1 travel contracts, a deterministic `TripRequest` to `PlannerContext` compiler, a typed AMap provider, schema-constrained Constraint/Explore/Stay Agents, a provider-grounded single-Planner baseline, a deterministic plan/budget validator, a fixture-backed complete Beijing three-day `TripPlan`, a SQLite-checkpointed main Graph using native LangGraph interrupt/resume, an independent parallel Explore/Stay/proactive-Weather information-gathering Graph, a bounded route matrix plus auditable budget allocator, a Plan Agent that consumes those materials into the shared `TripPlan` contract, a zero-model finalization gate, deterministic issue-directed retry/HITL routing with artifact-reuse guards, and a zero-model Weather Repair Coordinator that grades validated proposals before auto-apply or HITL.

The product-facing planning API now runs Product Graph V2: parallel Explore/Stay/proactive-Weather specialists feed deterministic route/budget materials, a schema-constrained Plan Agent, the full Hard Validator, a bounded responsibility-node Repair Router, checkpoint-backed HITL, and structured PlanVersion revision. Product repair can selectively rerun Explore, Stay, Route, Budget, or Plan while preserving unaffected artifacts and reporting delegated call counts. Task metadata and SSE logs remain process-local, and the separate Weather Repair Coordinator is not yet connected to this product Graph. EzTrip is an on-demand planner, so scheduled WeatherWatch is intentionally out of V1 scope. See [the planning task API protocol](../docs/api/planning-task-api.md).

The 30-case system-comparison inventory is frozen under `evals/cases/comparison`, with a deterministic report committed at `evals/reports/system-comparison-fixture.v1.json`. The full single-Agent arm and Product Graph without the hard gate each finalize 4/28 eligible fixtures; Product Graph with Hard Validator and bounded repair finalizes 20/28. The paired +16 recoveries measure only the validator/repair control path over designed development faults. They do not establish Specialist-model quality or a real-user success rate, and the replay performs no DeepSeek, AMap, or LangSmith calls.

The next repeated-live pilot protocol is frozen under `evals/cases/live-comparison`: three existing development cases, two repetitions each, shared Product initial drafts, sequential trials, and a hard ceiling of 54 model calls / 55,800 completion tokens. It reuses frozen Provider catalogs and plans zero AMap calls. The current preflight only validates local inventory, budget math, and key/tracing presence; it makes no DeepSeek, AMap, or LangSmith requests. No live comparison result is claimed yet.

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

Run the deterministic three-arm system comparison:

```powershell
uv run python -m scripts.export_comparison_eval_schemas
uv run python -m scripts.run_system_comparison_eval
uv run python -m scripts.plan_live_system_comparison
uv run pytest tests/test_comparison_evaluation_contract.py tests/test_system_comparison_evaluation.py --no-cov
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
uv run python -m scripts.export_planning_material_schemas
uv run python -m scripts.export_plan_agent_schemas
uv run python -m scripts.export_weather_repair_schemas
uv run python -m scripts.export_planning_task_schemas
uv run python -m scripts.export_plan_validation_example
uv run python -m scripts.export_planner_context_example
uv run python -m scripts.run_minimal_planning_graph --write-example
```

Run the planning task API and its real-SSE protocol tests:

```powershell
uv run uvicorn app.main:app --reload
uv run pytest tests/test_planning_task_api.py tests/test_planning_task_service.py --no-cov
```

The default `fixture` mode uses only allow-listed offline AMap captures and a deterministic
fixture scheduler. `live` mode is rejected unless `EZTRIP_PLANNING_LIVE_ENABLED=true`; enabling
it can call AMap and DeepSeek and consume quota. SSE emits committed graph-node events, heartbeat
comments, typed terminal failures, and supports `Last-Event-ID` replay within the same server
process.

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

The four-node subgraph asks the model for attraction/dining search strategies, calls only the injected POI provider, then asks the model to rank provider candidate IDs with typed evidence. Deterministic code owns IDs, facts, source lineage, deduplication and evidence validation. The six-case live report is a prompt-development regression result over fixture catalogs, not a real-time recommendation-accuracy or holdout score. Explore now participates in Product Graph V2 specialist fan-out and can be selectively rerun by product repair.

Run the Stay Agent tests offline or explicitly refresh its live development-set baseline:

```powershell
uv run pytest tests/test_stay_agent.py tests/test_stay_agent_evaluation.py --no-cov
uv run python -m scripts.run_stay_agent_eval --live
```

The four-node subgraph asks the model for accommodation-area search strategies, calls only the injected `StaySearchProvider`, then ranks Provider candidate IDs with typed evidence. Code owns candidate facts and rejects blocked contexts before model/Provider calls. The typed AMap adapter converts only hotel-classified POIs and leaves price empty, availability unknown and booking disabled. The six-case fixture-catalog report is a development regression result, not evidence of real-time hotel price, availability or recommendation quality. Stay now participates in Product Graph V2 specialist fan-out and can be selectively rerun by product repair.

Run the deterministic plan validator and regenerate its committed example:

```powershell
uv run pytest tests/test_plan_validator.py --no-cov
uv run python -m scripts.export_plan_validation_example
```

The base validator cross-checks request/plan identity, city and dates, duplicate candidates, recommendation source modes, and budget scope. Budget totals are recomputed from `Decimal` `CostItem` values; missing included categories remain incomplete instead of becoming zero-cost assumptions. Hard errors block finalization through typed `ValidationIssue` results. The downstream Hard Validator described below adds route feasibility, must/avoid, candidate city/lineage, and opening-hours evidence; Product Graph V2 passes those issues to Repair Router before HITL.

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

Run the deterministic route and budget material regression:

```powershell
uv run python -m scripts.export_planning_material_schemas
uv run python -m scripts.run_planning_material_eval
uv run pytest tests/test_planning_materials.py tests/test_planning_material_eval.py --no-cov
```

The material builder selects at most four ranked POIs and one primary stay, queries a directed transit matrix with concurrency capped at four, preserves typed per-edge failures, and allocates exact-cent budget targets with versioned weights. The committed fixture report records 5/5 cases and 42/42 expected route edges. The allocator creates planning targets rather than price facts; its bundle is consumed by the Plan Agent described below rather than being presented as an itinerary itself.

Run the multi-Agent Plan Agent grounding regression offline or explicitly refresh its live-model baseline:

```powershell
uv run python -m scripts.export_plan_agent_schemas
uv run python -m scripts.run_plan_agent_eval
uv run python -m scripts.run_plan_agent_eval --live
uv run pytest tests/test_plan_agent.py tests/test_plan_agent_evaluation.py --no-cov
```

The Plan Agent makes one schema-constrained placement call only for ready material bundles, while deterministic code preserves candidate facts, sources, route edges, weather risks, complete dates, IDs, and validation lineage. The committed fixture and DeepSeek reports both record 6/6 cases, 12/12 grounded and route-backed scheduled candidates, and two zero-model-call stops. Budget allocations remain targets rather than verified prices, so the Plan Agent emits no fabricated `CostItem`. Its isolated baseline does not evaluate finalization rules; the downstream Hard Validator does.

Run the deterministic Hard Validator regression:

```powershell
uv run python -m scripts.export_hard_validator_schemas
uv run python -m scripts.run_hard_validator_eval
uv run pytest tests/test_hard_validator.py tests/test_hard_validator_evaluation.py --no-cov
```

The Hard Validator requires a grounded Plan draft, its exact planning-material bundle, and a separate provider-backed opening-hours evidence bundle. It checks confirmed hard must/avoid rules, shortlist/source lineage, POI and stay city, route presence/endpoints/matrix lineage, transfer windows, opening-hours coverage, and the existing hard-budget assessment without any LLM call. The committed fixture report records 12/12 cases, 22/22 exact issue routings, and 12/12 deterministic replays. This proves rule and responsibility contracts over fixtures, not live opening-hours accuracy or automatic-repair quality.

Run the bounded Repair Router regression:

```powershell
uv run python -m scripts.export_repair_router_schemas
uv run python -m scripts.run_repair_router_eval
uv run pytest tests/test_repair_router.py tests/test_repair_router_evaluation.py --no-cov
```

The deterministic Router automatically processes errors only, groups them by typed repair action, prioritizes upstream actions, stops `ASK_USER` before any Agent call, and caps each action at two attempts. Executors must declare executed nodes; semantic fingerprints reject changes to reused Constraint/Explore/Stay/Weather/Route/Budget/Plan artifacts. The committed isolated fixture report records 9/9 exact action/node routes, retry bounds, reuse checks, and deterministic replays with zero Router model calls. Product Graph V2 now injects a real executor for Explore, Stay, Route, Budget, and Plan repair chains. Its default fixture resolves an opening-hours conflict with a deterministic same-day Plan repair and zero delegated model/provider calls; live quality remains provider- and model-dependent.

Run the proactive Weather Repair regression:

```powershell
uv run python -m scripts.export_weather_repair_schemas
uv run python -m scripts.run_weather_repair_eval
uv run pytest tests/test_weather_repair.py tests/test_weather_repair_evaluation.py --no-cov
```

The zero-model Coordinator matches significant provider risks to itinerary items using aware-time overlap and grounded activity environments. It creates a local task without user weather text, rejects changes to unrelated items, rechecks residual exposure and Hard Validator results, and caps delegated replanning at two attempts. Minor same-day changes may auto-apply; cross-day or multi-day changes retain the effective plan and expose a `pending_confirmation` proposal. The committed fixture report records 10/10 cases, five no-false-positive scenarios, five proactive tasks, one auto-apply, one HITL proposal, three bounded retry cases, full source traceability, and deterministic replay. Real responsibility-node repair executors remain product-Graph work; scheduled refresh is intentionally out of V1 scope.

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
