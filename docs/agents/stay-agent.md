# Stay Agent V1

EZ-203 implements an isolated LangGraph subgraph for accommodation-area discovery:

```text
propose_queries -> search_candidates -> select_candidates -> validate_selection
```

It is deliberately not wired into the stateful main graph yet. Parallel Explore/Stay/Weather fan-out and state merge belong to EZ-204.

## Model and code boundary

DeepSeek receives a structured `PlannerContext`, never credentials or raw AMap responses. Two forced-tool schemas limit the model to:

1. proposing one to three target-area and lodging-POI keyword searches, with references to known travel styles, party facts or confirmed constraint IDs;
2. ranking only candidate IDs returned by the injected `StaySearchProvider`, with short reasons and typed evidence references.

The model cannot set candidate names, city, district, address, coordinates, source metadata, stable IDs, prices, availability or booking capability. The selection payload deliberately omits all price fields, including a price estimate that might have a legitimate external source. V1 therefore does not let the model make price or budget claims.

Deterministic code:

- rejects blocked `STAY_SEARCH` contexts, unsupported destinations and missing room counts before model or Provider calls;
- exposes only `StaySearchProvider.search_stays` to this Agent, rather than the full travel-data interface;
- calls the Provider with at most three results per query;
- rejects duplicate IDs within a response, cross-city candidates and one ID reused for different facts;
- deduplicates candidates across searches while preserving every query ID as lineage;
- rejects unknown or repeated selected IDs and non-contiguous ranks;
- verifies each evidence value against the candidate's actual query IDs, area, district or tags;
- copies complete `CandidateStay` and `SourceReference` objects into the result.

The typed AMap adapter implements `search_stays` by calling text search and POI detail, filtering hotel-classified POIs and converting them into `CandidateStay`. AMap POI identity, location, district and categories can support an area-search shortlist. They do not provide a trusted OTA price or live inventory contract, so the adapter leaves price fields empty, `availability_status=unknown` and `booking_supported=false`.

The graph is compiled with `checkpointer=False`. The outer orchestration graph owns durable checkpoints; this subgraph contains model/Provider calls and has no human interrupt.

## Current output

`StayAgentResult` contains normalized area/search strategies, all deduplicated Provider observations and a ranked recommendation subset. It records separate model names, token usage and latency for query strategy and selection.

This is a source-traceable accommodation shortlist, not a booking result or a completed `TripPlan`. It does not verify room type, real-time price, inventory, rating, amenities, cancellation terms, route time, weather or budget feasibility.

## Verification

```powershell
Set-Location backend
uv run pytest tests/test_stay_agent.py tests/test_stay_agent_evaluation.py --no-cov
uv run python -m scripts.run_stay_agent_eval --live
```

Offline tests use injected models and explicitly labelled fixture candidates. The live command makes paid DeepSeek calls and uploads LangSmith traces, so CI never runs it.
