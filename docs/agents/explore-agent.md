# Explore Agent V1

EZ-202 implements an isolated LangGraph subgraph for open-ended attraction and dining discovery:

```text
propose_queries -> search_candidates -> select_candidates -> validate_selection
```

It is deliberately not wired into the stateful main graph yet. Parallel Explore/Stay/Weather fan-out and state merge belong to EZ-204.

## Model and code boundary

DeepSeek receives a structured `PlannerContext`, never raw provider responses or credentials. Two forced-tool schemas limit the model to:

1. proposing one to four `attraction` or `dining` keyword searches with references to known travel styles or confirmed constraint IDs;
2. ranking only candidate IDs returned by the injected `TravelDataProvider`, with short reasons and typed evidence references.

The model cannot set candidate names, city, address, coordinates, categories, source metadata, stable IDs or response hashes. Deterministic code:

- rejects blocked destinations, duplicate queries and unknown context references;
- calls only `TravelDataProvider.search_pois`, with at most three results per query;
- rejects duplicate IDs within one response, cross-city candidates and one ID reused for different facts;
- deduplicates candidates across queries while preserving every query ID as lineage;
- rejects unknown or repeated selected IDs and non-contiguous ranks;
- verifies each evidence value against the selected candidate's actual query IDs, categories, district, environment or tags;
- copies complete `CandidatePOI` facts and `SourceReference` objects into the result.

The graph is compiled with `checkpointer=False`. The outer orchestration graph owns durable checkpoints; this subgraph contains network/model calls and has no human interrupt.

## Current output

`ExploreAgentResult` contains normalized search queries, all deduplicated provider observations and a ranked recommendation subset. It also records separate model names, token usage and latency for query strategy and selection.

This is a candidate-discovery result, not a `TripPlan`. It does not verify opening hours, tickets, crowding, routes, weather, hotel availability or budget feasibility.

## Verification

```powershell
Set-Location backend
uv run pytest tests/test_explore_agent.py tests/test_explore_agent_evaluation.py --no-cov
uv run python -m scripts.run_explore_agent_eval --live
```

Offline tests use injected models and explicitly labelled fixture candidates. The live command performs paid DeepSeek calls and uploads LangSmith traces, so CI never runs it.
