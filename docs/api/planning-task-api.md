# Planning Task API V1

EZ-401 established the asynchronous FastAPI boundary, EZ-403A/403B added checkpoint HITL and a
bounded PlanVersion v2 revision, EZ-405 moved the task executor to Product Graph V2, EZ-405B
connected issue-directed product repair before HITL, EZ-406A added provider-backed destination
resolution, EZ-406B added a separate evidence-backed request-intake confirmation boundary, and
EZ-407A added a local durable task ledger plus URL-based recovery. The
planning-task endpoint still accepts an already structured `TripRequest`; Chinese free text reaches
it only after the client explicitly confirms a request-intake draft.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/request-intakes` | Propose evidence-backed request fields and constraints without creating a planning task |
| `POST` | `/api/request-intakes/{draft_id}/confirm` | Confirm the proposal or the explicit form and return a versioned `TripRequest` |
| `POST` | `/api/destinations/resolve` | Resolve a typed domestic administrative destination before planning |
| `POST` | `/api/planning-tasks` | Create a task and return `202 queued` |
| `GET` | `/api/planning-tasks/{task_id}` | Read the current typed snapshot |
| `GET` | `/api/planning-tasks/{task_id}/events` | Replay and follow the SSE event log |
| `POST` | `/api/planning-tasks/{task_id}/review-decisions` | Submit one idempotent review decision and return `202 running` |

The committed JSON Schema bundle is
[`evals/schemas/planning-task-api.v1.json`](../../evals/schemas/planning-task-api.v1.json).

## Request-intake confirmation boundary

`POST /api/request-intakes` receives the raw Chinese request, current structured form values,
reference date, and fixture/live mode. Request Intake and Constraint Agent outputs are proposals,
not authority: each proposed value must cite an exact raw-text evidence span, and deterministic code
recomputes or rejects dates, trip length, party counts, budget, city text, pace, and other string
values. The response exposes matched, conflict, proposed, unmentioned, and needs-confirmation field
states plus clarifications. It does not create a planning task.

The frontend may run one bounded destination-resolution preflight for the selected proposal/form.
After the user explicitly selects `proposal` or `form` and chooses an unambiguous administrative
candidate, `POST /api/request-intakes/{draft_id}/confirm` returns a `request-confirmation-*` ID and a
schema-constrained `TripRequest`. The subsequent planning-task request carries that confirmation ID.

Fixture intake is a bounded deterministic parser for committed browser and API scenarios, not a
general Chinese NLU claim. Live intake requires configured DeepSeek/LangSmith and has not been
validated by the EZ-406B local fixture gate. Pre-task draft records are process-local: a backend
restart returns `request-intake-not-found`, so the user must repeat request understanding if a
refresh happens before the planning task is confirmed and created.

## Durable task/session recovery

After `POST /api/planning-tasks` succeeds, the default backend atomically persists one versioned
record per task in `tmp/planning-task-store.sqlite3`. The record contains the typed snapshot,
contiguous SSE events, the accepted review decision, and its idempotency index. The frontend writes
the returned `task_id` to the URL. Opening or refreshing that URL reads the latest snapshot, replays
the event ledger, continues an in-flight SSE connection, restores an awaiting review, or renders a
completed review outcome.

Awaiting-input and terminal tasks survive backend reconstruction and retain exact review-decision
idempotency. A task found in `queued` or `running` during reconstruction instead receives a
retryable `planning-task-interrupted` failure. The server never automatically replays a possibly
paid model or Provider stage after process loss; the user starts a new task explicitly.

This is a local single-instance recovery contract. It does not provide multi-process cache
coherence, distributed worker ownership, encryption, retention cleanup, high availability, or an
outbox/exactly-once guarantee for external effects.

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

The bounded operations are `shift_day_later` and `replace_activity`. The client must build scope from the latest
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

For activity replacement, use the same complete target/protected scope and replace the operation fields with:

```json
{
  "operation": "replace_activity",
  "replaced_item_id": "plan-item-...",
  "replacement_candidate_id": "provider-observation-candidate-..."
}
```

For one weather confirmation that replaces every affected activity on the selected day, omit the
two singular fields and send a batch instead:

```json
{
  "operation": "replace_activity",
  "activity_replacements": [
    {
      "replaced_item_id": "plan-item-outdoor-1",
      "replacement_candidate_id": "provider-observation-indoor-1"
    },
    {
      "replaced_item_id": "plan-item-outdoor-2",
      "replacement_candidate_id": "provider-observation-indoor-2"
    }
  ]
}
```

The batch is atomic and date-scoped: every target and replacement candidate must be unique, all
targets must belong to `target_date`, and the server either rejects the request or applies the whole
batch in one new plan version. The revised draft still carries the complete validation report, so
missing opening-hours or route evidence remains visible. The original singular fields remain
available for the manual one-activity editor.

The replacement candidate must be an unscheduled, non-dining candidate from the persisted Explore
Provider observations. The server rejects stale bases with `409 revision-base-version-mismatch`,
scope drift with `409 revision-scope-mismatch`, and ineligible candidates with
`409 revision-replacement-not-eligible`.

The checkpoint revision node preserves every other day and protected plan fact. `shift_day_later`
reuses persisted materials and makes zero Provider/model calls. A single or batched
`replace_activity` recalculates the target-day route chain once, plus timing, nearby-meal
recommendations, deterministic budget allocation,
and `hard-trip-plan-validator-v1`; it records the incremental Provider call count and makes zero model
calls. Missing opening-hours evidence for the replacement remains a blocking validation issue. Both
operations record `v1 → v2` with an explicit item/date diff.

This is not an open-ended natural-language replan. It cannot invent a POI, arbitrarily remove one,
or broaden the observation set. Explore, Stay, Weather, Route/Budget, Plan, Hard Validator, and the
pre-review Repair Router are in the product task graph. Neither bounded revision operation re-enters
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
- `planning-materials-blocked` means the grounded Explore/Stay/Route materials were insufficient
  for a complete `TripPlan`. It is retryable and gives the user an actionable retry/keyword hint;
  generic protocol violations remain `planning-workflow-error`.
- Live weather risks enter the `TripPlan` only when their Provider date range overlaps the trip.
  AMap's short forecast horizon therefore produces an empty, explicit weather-risk set for a later
  trip instead of attaching stale dates or failing plan validation.
- Fixture is the frontend default. Selecting live mode is the explicit per-request opt-in; it can
  call AMap and DeepSeek and consume quota. Credentials remain server-side, and missing AMap or
  model credentials fail with typed configuration errors.

## Current durability boundary

LangGraph planning state remains persisted per task in ignored local SQLite checkpoint files, and
the review endpoint resumes that checkpoint. A separate local SQLite task ledger now persists API
metadata, accepted-decision indexes, and the SSE event log across a single backend restart. The two
stores are deliberately local portfolio/runtime evidence: multi-worker coordination, cleanup and
retention policy, encryption, broader selective replanning, and a production outbox remain later
product work.
