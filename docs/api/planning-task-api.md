# Planning Task API V1

EZ-401 exposes the existing SQLite-checkpointed Gate 2 workflow through an asynchronous FastAPI
boundary. The API accepts an already structured `TripRequest`; Chinese free-text extraction and the
newer specialist/Plan/Repair/Weather orchestration are not silently implied by this endpoint.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/planning-tasks` | Create a task and return `202 queued` |
| `GET` | `/api/planning-tasks/{task_id}` | Read the current typed snapshot |
| `GET` | `/api/planning-tasks/{task_id}/events` | Replay and follow the SSE event log |

The committed JSON Schema bundle is
[`evals/schemas/planning-task-api.v1.json`](../../evals/schemas/planning-task-api.v1.json).

## Offline fixture request

Start the API from `backend/`:

```powershell
uv run uvicorn app.main:app --reload
```

The default fixture contains allow-listed Beijing captures for the Palace Museum and Temple of
Heaven. The following two-day request therefore runs without API keys or network access:

```powershell
$body = @'
{
  "request": {
    "request_id": "local-api-beijing-v1",
    "raw_text": "两位成年人去北京玩两天，必须去故宫和天坛公园。",
    "destination_city": "北京市",
    "start_date": "2026-10-02",
    "end_date": "2026-10-03",
    "party": {"adults": 2},
    "constraints": {
      "items": [
        {
          "constraint_id": "must-visit-forbidden-city",
          "kind": "must_visit",
          "value": "故宫",
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
graph_node_completed (run_vertical_slice, state=plan_ready)
graph_node_completed (prepare_human_review, state=awaiting_human_review)
task_awaiting_input
```

The stream stops at `awaiting_input` because the approval/resume HTTP endpoint belongs to EZ-403.
Heartbeat comments (`: heartbeat`) keep an idle connection alive but are not planning progress.

## Reconnect and failure rules

- Each event has a contiguous sequence and stable ID such as
  `planning-task-...-event-000003`.
- Send `Last-Event-ID` when reconnecting; the server returns only later events. `?after=N` is
  available for clients that cannot set the header.
- A cursor from another task, malformed cursor, future cursor, or conflicting header/query returns
  a typed `400 invalid-event-cursor` response.
- Provider, configuration, workflow, timeout, and internal failures return stable codes and
  user-safe messages. Raw exception text is not copied into snapshots or SSE.
- Live mode is disabled by default. Set `EZTRIP_PLANNING_LIVE_ENABLED=true` deliberately; this can
  call AMap and DeepSeek and consume quota.

## Current durability boundary

LangGraph planning state is persisted per task in ignored local SQLite checkpoint files. Task
metadata and the SSE event log are currently stored in process memory, so reconnect replay works
within one running API process but not after a server restart. Durable task/event persistence,
multi-worker coordination, cancellation, and API-level HITL resume remain later product work.
