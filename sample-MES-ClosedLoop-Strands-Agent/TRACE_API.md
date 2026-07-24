# Trace API — the "under the hood" contract for frontends

The FastAPI backend (`api.py`, `http://127.0.0.1:8000`, started with
`python startup.py --api`) exposes everything the agent system does — per-agent
streamed text, every tool call with arguments/results/durations, the literal
SQL each tool ran, and run status — over three endpoints. `trace_viewer.py`
(Streamlit, `streamlit run trace_viewer.py --server.port 8502`) is a working
reference client for this exact contract; a Next.js consumer can be built
against the same endpoints without any backend changes (CORS already allows
`http://localhost:3000` for GET and POST).

## Endpoints

### `GET /health`

Always answers 200, even when the agent side is broken — this is the first
debugging stop. Never contains the API key itself.

```json
{
  "status": "ok",
  "model_id": "claude-sonnet-4-6",
  "db_path": "C:\\...\\sample-MES-ClosedLoop-Strands-Agent\\mes.db",
  "db_exists": true,
  "anthropic_api_key_set": true,
  "agent_manager_ready": true,
  "agent_manager_error": null
}
```

If the manager failed to build (missing key, bad config), `agent_manager_ready`
is `false` and `agent_manager_error` holds the exception text; `/chat/` will
return 503 until it's fixed and the server restarted.

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

## `POST /chat/`

Unchanged request/response: `{"user_input": "..."}` → `{"analysis": "..."}`.
The call blocks for the whole run (minutes); poll `/trace` in parallel for
live progress. Errors are FastAPI-standard `{"detail": "..."}`:

| status | meaning |
|---|---|
| 409 | a run is already in progress (one traced run at a time) |
| 500 | the run failed; the trace also ends with `error` + `run_end{status:"failed"}` |
| 503 | agent manager not ready — see `GET /health` for why |
