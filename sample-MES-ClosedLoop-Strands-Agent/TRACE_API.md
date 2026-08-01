# Trace API — the "under the hood" contract for frontends

The FastAPI backend (`api.py`, `http://127.0.0.1:8000`, started with
`python startup.py --api`) exposes everything the agent system does — per-agent
streamed text, every tool call with arguments/results/durations, the literal
SQL each tool ran, and run status — over three endpoints. `trace_viewer.py`
(Streamlit, `streamlit run trace_viewer.py --server.port 8502 --server.address
127.0.0.1`) is a working
reference client for this exact contract; a Next.js consumer can be built
against the same endpoints through authenticated server-side proxy routes.

## Authentication and isolation

Every endpoint below except `GET /health` requires the server-only header
`X-MES-Internal-Token`. The configured token must be at least 32 characters;
missing configuration returns `503`, and an invalid or missing caller token
returns `401`. Never expose this token to browser JavaScript.

Next.js validates the Better Auth session before proxying a request and adds
`X-MES-User-ID` from that validated session to `/chat/`, `/trace`, and
`/cancel`. The backend uses that value to prevent one signed-in user from
observing or cancelling another user's run. The browser cannot choose the
header value. Trusted loopback clients such as `trace_viewer.py` omit the user
header and act as operator clients.

For chat, Next.js also verifies that `conversation_id` belongs to the
authenticated user and loads the saved transcript from PostgreSQL. The
browser cannot supply chat history directly to this service.

## Endpoints

### `GET /health`

Answers `200` when ready and `503` when unavailable. It deliberately exposes
no paths, model names, credentials, or exception text.

```json
{
  "status": "ok",
  "agent_manager_ready": true
}
```

If manager construction fails, the response is
`{"status":"unavailable","agent_manager_ready":false}` with status `503`.
Diagnostic details remain in server logs.

### `GET /trace?since=N`

Poll this (~800 ms works well) to render the live trace. Returns events with
`seq > N` plus overall run state:

```json
{
  "seq": 137,
  "events": [ { "seq": 101, "ts": 1753257601.2, "iso": "2026-07-23T08:00:01+00:00",
                "agent": "Analyzer", "kind": "tool_start", "...": "..." } ],
  "run": { "status": "running", "run_id": "9f3c21ab",
           "label": "Chat: why are scratch defects up?",
           "started_at": "2026-07-23T08:00:00+00:00", "started_ts": 1753257600.0 },
  "current": { "agent": "Analyzer", "tool": "get_defect_data" }
}
```

A signed-in user's server-side proxy receives `403` when the retained trace
belongs to another user or to an operator-triggered run.

- `run.status`: `idle` | `running` | `completed` | `failed`. When ended, `run`
  also has `ended_at`, `duration_ms`, and (on failure) the `run_end` event
  carries `error`.
- `current` is what is executing *right now* (both fields `null` when nothing is).
- The buffer holds one run (max 5000 events; oldest trimmed on very long runs).

**Cursor protocol / reset detection.** Keep the last `seq` you saw and pass it
as `since`. Two situations replace the buffer under you:

1. A new run started (`tracer.reset()`), or
2. the server restarted (e.g. uvicorn `--reload` on a code edit).

Detect them with: **if the returned `seq` is lower than your cursor, drop your
cursor to 0 and clear your accumulated view**; additionally, **whenever a
`run_start` event appears in a batch, clear the accumulated view** (covers the
rare case where a new run has already produced more events than your cursor).
`run.run_id` changes on every run and is a convenient key for the same check.
On fetch failure, keep the last view, mark the backend disconnected, and keep
polling — it recovers by itself.

## Event kinds

Every event has `seq` (monotonic int), `ts` (unix float), `iso` (UTC string),
`agent` (`"Supervisor" | "Monitor" | "Analyzer" | "Planner" | "Verifier" |
"Executor"` or `null` for run-level events), and `kind` plus kind-specific
fields:

| kind | extra fields | meaning |
|---|---|---|
| `run_start` | `label`, `params`, `run_id` | a traced run began |
| `run_end` | `status`, `duration_ms`, `error` | it finished (`completed`/`failed`) |
| `agent_start` | — | this agent began working |
| `agent_end` | — | it finished |
| `text` | `text`, `reasoning: bool` | text the agent streamed (`reasoning: true` = its chain-of-thought, dimmer styling suggested) |
| `tool_start` | `tool_name`, `tool_input`, `tool_use_id` | a tool call began, with its arguments |
| `tool_end` | `tool_name`, `tool_use_id`, `status`, `row_count`, `result_preview`, `duration_ms` | it returned; join to its `tool_start` via `tool_use_id` (the end may arrive after intervening events from sub-agents) |
| `query` | `tool_name`, `sql`, `params`, `row_count`, `execution_time_ms`, `ok`, `error` | the literal SQL a tool ran against mes.db |
| `error` | `message` | something went wrong (also emitted before a failed `run_end`) |

Long blobs are pre-truncated server-side (text ≤ 6000 chars, previews/SQL
≤ 4000) with an explicit `… (truncated, N chars total)` suffix.

A rendering approach that works well (see `trace_viewer.py`): group events by
consecutive `agent` value into collapsible sections (null → a "Run" section),
render `tool_start` as a card enriched with its matched `tool_end`
(duration/status/row_count/result), and nest `query` events under the tool that
ran them.

## `GET /defect-types?days_back=365`

Distinct defect types from the last `days_back` days (default 365), for UI
dropdowns: `{"defect_types": ["crazing", "inclusion", ...]}`. 503 while the
agent manager isn't ready.

## `GET /alerts?limit=20`

Alerts the CV pipeline raised (CONTRACTS.md §3), newest first, so the
dashboard can show camera-triggered investigations without opening its own
database connection.

```json
{"alerts": [{
  "AlertID": 1, "CreatedAt": "2026-07-28 23:41:04-07:00",
  "MachineID": 1, "OrderID": 4901,
  "DefectType": "patches", "DetectionCount": 6,
  "WindowStart": "...", "WindowEnd": "...",
  "Status": "done", "Report": "## Root Cause Investigation Report…",
  "CompletedAt": "...", "DurationSeconds": 37.0
}]}
```

`Status` is one of `pending`, `analyzing`, `done`, `failed`, `cancelled`.
`DurationSeconds` is `CreatedAt`→`CompletedAt`, null until the run finishes.

Deliberately **not** behind the one-run-at-a-time guard: reading the alert
list has to keep working while an investigation is in flight, which is
exactly when someone is watching it. Cheap to poll — a plain SELECT, no
model call. On SQLite, or before the bridge has ever run, returns
`{"alerts": [], "note": "..."}` rather than an error: no alerts yet is a
normal state, not a failure. 503 while the agent manager isn't ready.

## `POST /analysis`

The structured entry point the dashboard uses. Scope flags arrive as
parameters rather than prose, and the backend builds the supervisor prompt
(including a verified window pre-check) itself.

```json
{"defect_type": "Battery Cell Variance", "days_back": 7,
 "include_oee": false, "include_downtime": false,
 "include_changeover": false, "include_maintenance": true}
```

Returns the full analysis dict — `status` (`completed`/`failed`),
`total_duration`, `supervisor_orchestration` (the final report text),
`analysis_scope`, and timestamps. Blocks for the whole run (minutes); poll
`/trace` in parallel. Same error codes as `/chat/` (409 busy, 503 not ready).

Look-back windows count back from the **newest record in the database**, not
from today, so a "last 7 days" run always overlaps the data.

## `POST /chat/`

Request:

```json
{
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_input": "Do it on some random machines",
  "history": [
    {"role": "user", "content": "maintenance correlation"},
    {"role": "assistant", "content": "Which machines should I analyze?"}
  ]
}
```

`conversation_id` is a UUID. `user_input` is nonblank and at most 4,000
characters. `history` is required, contains at most 20 messages as complete,
alternating user/assistant pairs, and is limited to 64 KiB of UTF-8 text. The
authenticated Next.js proxy constructs this bounded history from the
owner-scoped PostgreSQL conversation. The backend replaces all in-process
chat and workflow history from it on every request, so chat switches and
backend restarts do not lose or leak context.

A successful call returns an NDJSON stream
(`application/x-ndjson`) so long reports do not look like an idle HTTP
connection:

```jsonl
{"type":"started"}
{"type":"heartbeat"}
{"type":"result","data":{"analysis":"..."}}
```

That is the internal Python response. The authenticated Next.js proxy saves the
assistant text to the same owner-scoped PostgreSQL conversation before exposing
success to the browser, then adds the server-generated saved-message ID:
`{"type":"result","data":{"analysis":"...","messageId":"<uuid>"}}`. The browser
cannot submit that assistant row itself.

Heartbeats arrive every 10 seconds until the terminal `result` or `error`
event. Cancellation ends with
`{"type":"result","data":{"status":"cancelled"}}`. The run guard is released
before the terminal event is emitted, so completion also means the API is
ready for the next prompt. Poll `/trace` in parallel for detailed live
progress. `POST /cancel` signals the chat agent, Supervisor, and any active
specialist, then prevents later phases and retries from starting. Cancellation
is cooperative: Python already executing inside a tool, or a model request
that has not started streaming, may still wait for its configured timeout.
Errors detected before streaming starts remain FastAPI-standard
`{"detail": "..."}`:

| status | meaning |
|---|---|
| 400 | invalid input or missing server-derived user identity |
| 401 | invalid internal service credentials |
| 403 | the active/retained run belongs to another signed-in user |
| 409 | a run is already in progress (one traced run at a time) |
| 429 | rolling hourly run budget exhausted; respect `Retry-After` |
| 503 | agent manager or internal authentication is not configured |

Once streaming starts, a run failure is delivered as
`{"type":"error","error":"The analysis failed"}` and the trace ends with a
generic failed run marker. Detailed exceptions remain in server logs.
