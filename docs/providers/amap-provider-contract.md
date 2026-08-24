# AMap provider contract

## Purpose

EzTrip does not expose AMap MCP payloads to Agent state. The provider boundary accepts typed requests and returns the existing V1 domain DTOs:

| Provider operation | Domain output | Deterministic normalization |
|---|---|---|
| REST destination resolution | `DestinationResolution` | qualified name, planning city, administrative level, adcode, ambiguity, source hash |
| POI text search + detail | `tuple[CandidatePOI, ...]` | provider ID, coordinates, categories, indoor/outdoor classification, source hash |
| MCP weather + REST freshness | `tuple[WeatherRisk, ...]` | rain/snow/heat/cold/wind thresholds, severity, local date range, report time |
| walking/transit route | `RouteLeg` | endpoint validation, meters, seconds-to-minutes conversion, source hash |

POI, weather, and route live/fixture transports implement the same internal `AmapToolClient`
contract. `AmapTravelDataProvider` owns their normalization and returns `DataMode.LIVE` or
`DataMode.FIXTURE` sources without changing the DTO shape. Destination identity has a separate
`CityResolverProvider`: live mode uses AMap REST geocoding and fixture mode supplies deterministic
resolution, ambiguity, and unsupported outcomes.

## Reliability boundary

- The product path resolves and validates the destination through AMap REST before opening the official MCP session. Weather freshness is then fetched lazily for the selected adcode instead of probing a hard-coded city during client startup.
- MCP initialization and every tool call have explicit timeouts.
- The live client admits at most four MCP tool calls per 1.1-second rolling window. A 2026-08-24 controlled probe observed that an immediate fifth transit call returned only an MCP tool error, while the same call succeeded after a 1.2-second wait. This is a client safety limit, not a claim about the account's documented quota.
- Only `timeout` and `rate_limited` failures are retried. The default is at most two attempts with bounded exponential backoff.
- Authentication, missing fields, empty results and unrecoverable protocol failures are not silently retried.
- MCP decoding prefers a mapping in `structuredContent`, otherwise scans text blocks for a JSON object and accepts an exact `json` code fence. Plain-text tool errors remain typed failures and raw text is not copied into Agent state.
- A malformed or unavailable detail for one POI is isolated. Other valid details from the same search remain usable; a query-level detail failure does not discard candidates already grounded by other queries. If every candidate fails, the typed Provider failure is preserved.
- Product live execution opens separate AMap sessions for specialist collection, route-material construction, and any bounded repair rerun. Each session's client limiter controls calls inside that stage, so one burst cannot erase the remaining route matrix.
- Repeated search hits with the same provider ID are merged across Agent queries. Retrieval timestamps and response hashes may differ; normalized name, address, coordinates, categories, tags, and other candidate facts still must agree or the Agent raises a protocol error.
- POI detail IDs and route endpoints must match the request before data can enter the domain layer.
- MCP and REST weather responses must refer to the requested adcode and cover the same forecast dates.
- A browser-selected adcode must still exist in the server-side resolver result; the model and client cannot invent or substitute administrative codes.
- Weather risks are produced by ordinary code thresholds, not inferred by the LLM.

## Live and fixture modes

The offline command replays the versioned Beijing fixture and requires no Key or network:

```powershell
Set-Location backend
uv run python -m scripts.run_amap_provider_smoke
```

The live command uses the local `.env`, creates one MCP session, and prints only a safe domain summary:

```powershell
uv run python -m scripts.run_amap_provider_smoke --live
```

On 2026-08-20, both modes produced the same fixed Beijing contract shape:

- 故宫博物院 → `mixed` activity candidate;
- 天坛公园 → `outdoor` activity candidate;
- three `medium` rain risks for 2026-08-21 through 2026-08-23, discovered from provider weather data;
- walking route `5508 m / 74 min`;
- transit route `5172 m / 64 min`.

These are point-in-time contract observations, not current travel advice or performance metrics.

## Contract tests

`test_amap_provider_contract.py` runs the same POI, weather and route assertions against fixture and live-mode replay. Additional tests inject:

- timeout followed by rate limiting, verifying bounded backoff;
- terminal authentication failure, verifying zero retries;
- unrecorded fixture requests;
- missing POI coordinates;
- unsupported route modes;
- malformed fixture files;
- live MCP transport timeout;
- REST HTTP 429 and AMap `10001` authentication errors;
- non-Beijing weather requests, verifying that the requested adcode is used.
- structured, multi-block, fenced, plain-text error, and malformed POI detail MCP responses;
- partial POI and query success, verifying that one bad detail does not erase grounded candidates.
- the four-call rolling-window limiter and stage-scoped live Provider lifecycle.

## Current limits

- Product Graph V2 consumes the typed provider in explicit live mode, but live quality remains dependent on AMap coverage and DeepSeek output; CI never exercises that network path.
- The low-level committed AMap replay fixture records one top detail for each of two Beijing POI queries. The separate product fixture adds deterministic Shanghai and Chengdu scenarios, but neither fixture proves nationwide live quality.
- V1 exposes walking and transit routes through the domain port. Driving and cycling are intentionally rejected until their response contracts are captured and tested.
- Hotel candidates, price estimates and inventory are outside this adapter increment. AMap POI data still must not be presented as real-time hotel price or availability.
