# Planning Task API V1

EZ-401 established the asynchronous FastAPI boundary, EZ-403A/403B added checkpoint HITL and a
bounded PlanVersion v2 revision, EZ-405 moved the task executor to Product Graph V2, EZ-405B
connected issue-directed product repair before HITL, and EZ-406A added provider-backed destination
resolution. The API accepts an already structured `TripRequest`; Chinese free-text extraction is
not silently implied by this endpoint.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/destinations/resolve` | Resolve a typed domestic administrative destination before planning |
| `POST` | `/api/planning-tasks` | Create a task and return `202 queued` |
| `GET` | `/api/planning-tasks/{task_id}` | Read the current typed snapshot |
| `GET` | `/api/planning-tasks/{task_id}/events` | Replay and follow the SSE event log |
| `POST` | `/api/planning-tasks/{task_id}/review-decisions` | Submit one idempotent review decision and return `202 running` |

The committed JSON Schema bundle is
[`evals/schemas/planning-task-api.v1.json`](../../evals/schemas/planning-task-api.v1.json).

## Destination resolution

The frontend first sends `input_name` and `data_mode` to `/api/destinations/resolve`. A resolved
response contains one canonical `planning_city_name`, six-digit `administrative_code`,
administrative level, qualified name, and source. An ambiguous response contains multiple
candidates and does not create a planning task until the user selects one. `no_result`,
`unsupported`, configuration failure, and Provider failure remain distinct outcomes.

The planning executor resolves the city again and checks `selected_destination_adcode`; it does not
trust a browser-supplied code without Provider confirmation. Fixture resolution and planning cover
Beijing, Shanghai, and Chengdu plus a deterministic `朝阳` ambiguity case. Live resolution uses AMap
REST geocoding, after which POI, stay, route, and weather operations share the selected city name and
adcode. This is an input/routing capability for AMap-resolvable domestic destinations, not evidence
that every city has equal candidate quality. A single task still supports one destination only.

## Offline fixture request

Start the API from `backend/`:

```powershell
uv run uvicorn app.main:app --reload
```

The default product fixture contains explicitly synthetic, source-labelled Beijing, Shanghai, and
Chengdu planning data. The following Beijing two-day request therefore runs without API keys or
network access:

```powershell
$body = @'
{
  "request": {
    "request_id": "local-api-beijing-v1",
    "raw_text": "两位成年人去北京玩两天，必须去故宫和天坛公园。",
    "destination_city": "北京市",
    "start_date": "2026-10-02",
    "end_date": "2026-10-03",
    "party": {"adults": 2, "rooms": 1},
    "budget": {
      "total_limit": "3000",
      "included_categories": ["transport", "food", "admission", "activity"],
      "hard_limit": false
    },
    "constraints": {
      "items": [
        {
          "constraint_id": "must-visit-forbidden-city",
          "kind": "must_visit",
          "value": "故宫博物院",
          "strength": "hard",
          "priority": 5,
          "source": "user_explicit",
          "confirmed": true
        },
        {
          "constraint_id": "must-visit-temple-of-heaven",
          "kind": "must_visit",
          "value": "天坛公园",
          "strength": "hard",
          "priority": 5,
          "source": "user_explicit",
          "confirmed": true
        }
      ]
    }
  },
  "selected_destination_adcode": "110000",
  "data_mode": "fixture"
}
'@

$accepted = Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/planning-tasks `
  -ContentType application/json `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))

$accepted
Invoke-RestMethod -Uri ("http://localhost:8000" + $accepted.task_url)
curl.exe -N ("http://localhost:8000" + $accepted.events_url)
```

The normal event order is:

```text
task_created
task_started
graph_node_completed (run_specialists, state=planning)
graph_node_completed (build_materials, state=planning)
graph_node_completed (run_plan_agent, state=planning)
graph_node_completed (validate_hard_plan, state=plan_ready)
graph_node_completed (run_repair, state=plan_ready)
graph_node_completed (prepare_human_review, state=awaiting_human_review)
task_awaiting_input
```

`run_repair` is conditional: it is committed only when Hard Validator returns at least one error.
Warnings never start the automatic loop. The default fixture deliberately produces a repairable
opening-hours conflict, so its first stream contains nine events and one `run_repair` event.

`run_vertical_slice` belongs to the legacy checkpoint baseline and is no longer emitted by the
default product task executor. Product snapshots expose `specialists`, `materials`, `plan_agent`,
`plan`, the `hard-trip-plan-validator-v1` report, and an optional `repair` result with attempts,
executed/reused nodes, issue diffs, retry counts, and delegated model/provider counts.

The first stream stops at `awaiting_input`. Heartbeat comments (`: heartbeat`) keep an idle
connection alive but are not planning progress.

## Human-review resume

Read `result.state.review_request.review_id` from the awaiting-input snapshot, generate a stable
`decision_id` on the client, and submit one of the actions allowed by that pending review:

```powershell
$decision = @{
  decision_id = "decision-local-001"
  review_id = "<review_id from the task snapshot>"
  action = "approve_draft"
  reviewer_id = "local-user"
} | ConvertTo-Json

$acceptedDecision = Invoke-RestMethod `
  -Method Post `
  -Uri ("http://localhost:8000/api/planning-tasks/" + $accepted.task_id + "/review-decisions") `
  -ContentType application/json `
  -Body ([Text.Encoding]::UTF8.GetBytes($decision))

curl.exe -N ("http://localhost:8000" + $acceptedDecision.events_url + "?after=9")
```

The four protocol actions are `approve_draft`, `acknowledge_conflict`, `request_revision`, and
`cancel`. The current pending review determines which actions are allowed. `request_revision`
requires a non-empty `comment` of at most 500 characters and a confirmed structured
`revision_request`.

An accepted decision produces the following continuation events without replaying Provider or
Planner nodes:

```text
task_review_submitted
graph_node_completed (human_review, state=review_decided)
graph_node_completed (apply_review_decision, terminal review state)
task_succeeded
```

`request_revision` has one additional committed node and event:

```text
task_review_submitted
graph_node_completed (human_review, state=review_decided)
graph_node_completed (apply_review_decision, state=revision_requested)
graph_node_completed (apply_plan_revision, state=revision_applied)
task_succeeded
```

The client must reuse the same `decision_id` when retrying an ambiguous network result. An exact
replay returns `idempotent_replay=true` and does not start another resume worker. Reusing the same
ID for different content returns `409 review-decision-idempotency-conflict`; a second distinct
decision returns `409 review-already-decided`. Wrong task state, review ID, or disallowed action
also return stable typed `409` errors.

Every generated draft is captured as `plan_versions[0]` (`v1`) with constraint, tool snapshot,
model, prompt, and changed-date lineage. Approve, acknowledge, and cancel preserve that plan, so
the terminal `review_outcome.plan_diff` records `v1 → v1`, `plan_changed=false`, and zero changed
dates.

### Structured revision request

The current bounded operation is `shift_day_later`. The client must build its scope from the latest
`PlanVersion`: all item IDs on the selected date are targets, and every item on all other dates is
protected. For example:

```json
{
  "decision_id": "decision-local-revision-001",
  "review_id": "human-review-...",
  "action": "request_revision",
  "reviewer_id": "local-user",
  "comment": "第二天想晚一点出发。",
  "revision_request": {
    "revision_id": "revision-local-001",
    "base_version_id": "plan-version-...",
    "base_plan_id": "trip-plan-...",
    "target_date": "2026-10-03",
    "operation": "shift_day_later",
    "shift_minutes": 120,
    "target_item_ids": ["itinerary-item-day-two"],
    "protected_item_ids": ["itinerary-item-day-one"],
    "confirmed": true
  }
}
```

The server rejects stale base versions with `409 revision-base-version-mismatch` and incomplete or
drifted scope with `409 revision-scope-mismatch`. The checkpoint revision node then shifts exactly
the target-day items, rejects cross-date timestamps, preserves all other days and plan facts,
re-runs `hard-trip-plan-validator-v1` against persisted materials and opening-hours evidence, and
records `v1 → v2` with changed dates and rescheduled item IDs. It makes zero Provider and model
calls.

This is not an open-ended natural-language replan. It does not add/remove POIs or recalculate
routes/opening hours. Explore, Stay, Weather, Route/Budget, Plan, Hard Validator, and the pre-review
Repair Router are in the product task graph. The narrow `shift_day_later` revision does not re-enter
the automatic repair loop; its returned v2 remains a draft that has not been reviewed again.

## Reconnect and failure rules

- Each event has a contiguous sequence and stable ID such as
  `planning-task-...-event-000003`.
- Send `Last-Event-ID` when reconnecting; the server returns only later events. `?after=N` is
  available for clients that cannot set the header.
- A cursor from another task, malformed cursor, future cursor, or conflicting header/query returns
  a typed `400 invalid-event-cursor` response.
- Provider, configuration, workflow, timeout, and internal failures return stable codes and
  user-safe messages. Raw exception text is not copied into snapshots or SSE.
- Fixture is the frontend default. Selecting live mode is the explicit per-request opt-in; it can
  call AMap and DeepSeek and consume quota. Credentials remain server-side, and missing AMap or
  model credentials fail with typed configuration errors.

## Current durability boundary

LangGraph planning state is persisted per task in ignored local SQLite checkpoint files, and the
review endpoint resumes that checkpoint. Task metadata, accepted-decision indexes, and the SSE
event log are currently stored in process memory, so API reconstruction and idempotency do not
survive a server restart. Durable task/event/decision persistence, multi-worker coordination,
worker cancellation, broader selective replanning, and a production outbox remain later product work.
