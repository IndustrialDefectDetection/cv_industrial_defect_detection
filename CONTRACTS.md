# CONTRACTS.md — CV → MES Agentic Defect Detection

**Status: DRAFT (A) — needs B's approval.** Once approved, changing anything here requires telling the other person first.

All field and column names below are taken from the real code (`deployment/api.py`, `sqlite-synthetic-mes-data.py`), not invented — do not rename them.

## 1. Ports and processes

| Port | Process | Repo | Endpoint(s) |
|------|---------|------|-------------|
| 8080 | Inference API (`deployment/api.py`) | steel-defect-detection-mlops | `POST /predict`, `GET /health` |
| 8081 | Bridge (FastAPI, new) | industrial-data-store-simulation-chatbot | `POST /detection`, `GET /health` |
| 8501 | Streamlit dashboard (existing app + new tab) | industrial-data-store-simulation-chatbot | — |

Database: `mes.db` at the **root of industrial-data-store-simulation-chatbot** (the existing `DatabaseManager` resolves it relative to the working directory — bridge, agent, and dashboard must all be started from that repo's root). Enable WAL mode on connect (`PRAGMA journal_mode=WAL`).

### Internal-service security

- `Launch.py` binds locally served UIs to `127.0.0.1` and supplies one
  high-entropy `MES_INTERNAL_API_TOKEN` to the inference API, bridge, agent API,
  server-side Next.js routes, and trace viewer.
- Every operational inference, bridge, and agent endpoint requires
  `X-MES-Internal-Token`. Public health endpoints expose readiness only.
- The token is never sent to browser JavaScript. Next.js first validates the
  Better Auth session and then adds the token while proxying server-side.
- Next.js also adds `X-MES-User-ID` from the validated session to chat, trace,
  and cancel calls. It must never accept this ID from the browser. The agent API
  uses it to prevent one signed-in user from tracing or cancelling another
  user's run.
- Tokens must contain at least 32 characters. Missing or short tokens fail
  closed; do not add an insecure fallback.

### Authenticated chat context

- Before inference, the browser appends exactly one pending user message
  through `POST /api/chats` as
  `{"conversationId":"<uuid, omitted for a new chat>","message":{"id":"<uuid>","role":"user","text":"..."}}`.
  That authenticated route creates or locks the owner-scoped PostgreSQL
  conversation, appends at the next position, and returns its server-issued
  conversation UUID plus the authoritative ordered transcript. It is
  idempotent by user-message ID and never accepts an assistant message or a
  browser-supplied replacement transcript.
- The browser then calls `POST /api/chat` with exactly
  `{"conversationId":"<uuid>","messageId":"<uuid>","user_input":"..."}`.
  It never sends prior messages or a user ID.
- Next.js verifies `conversation.user_id` against the Better Auth session,
  requires the newest stored message ID and text to match the current user
  input, excludes that current message, and loads at most the 20 newest prior
  messages that form complete user/assistant pairs, with at most 64 KiB of
  UTF-8 content. Missing, non-owned, or changed conversations fail closed
  instead of running without the intended context.
- Next.js forwards the internal agent request as
  `{"conversation_id":"<uuid>","user_input":"...","history":[{"role":"user|assistant","content":"..."}]}`.
  The Python backend replaces its conversational and workflow-agent histories
  from that bounded transcript before every turn. PostgreSQL, not process
  memory, is the durable source of chat context.
- When the Python stream returns an analysis, Next.js verifies that the same
  pending user message is still newest, writes a server-generated assistant
  message into that owned conversation, and only then forwards the terminal
  result with the saved `messageId`. Cancelled and failed runs do not create an
  assistant row. A browser can display assistant text but cannot author it.
- A conversation stores at most 80 messages and 256,000 UTF-8 bytes. User
  prompts are limited to 4,000 JavaScript string units, and one assistant slot
  with up to 64,000 UTF-8 bytes is reserved before starting inference.
  Historical rows created before this server-authored boundary are legacy data;
  deployments that require verified provenance must migrate or clear those rows
  separately.

## 2. Detection payload — simulator → bridge

The simulator POSTs one JSON object to `http://localhost:8081/detection` per analyzed image. `detections` is passed through **unchanged** from the API's `/predict` response; the simulator adds the envelope fields.
Both the simulator's `/predict` and `/detection` requests carry the internal
service header described in §1.

```json
{
  "timestamp": "2026-07-19T14:02:31Z",          // simulator adds, UTC ISO-8601
  "machine_id": 12,                              // simulator adds (a real MachineID, see §4)
  "image_name": "scratches_101.jpg",
  "inference_time_ms": 42.1,
  "detections": [
    {
      "class": "scratches",                      // one of: crazing, inclusion, patches,
      "class_id": 5,                             //   pitted_surface, rolled-in_scale, scratches
      "confidence": 0.8712,
      "bbox": { "x1": 10.5, "y1": 20.0, "x2": 88.2, "y2": 95.1 }
    }
  ]
}
```

Validation limits: `/predict` accepts only decoded JPEG/PNG images, at most
10 MiB and 16 megapixels; `/batch-predict` accepts at most 16 files and
32 MiB total. The bridge accepts at most a 1 MiB JSON body and 100 detections
per payload. Timestamps must be timezone-aware, `image_name` must be a plain
filename, bounding boxes must be finite and ordered, and every `class_id` must
match its six-class `class` value.
The API and standalone Streamlit UI verify `best.pt` against the reviewed
`MODEL_SHA256` before deserializing the executable PyTorch artifact.

## 3. Database tables (bridge creates with `CREATE TABLE IF NOT EXISTS` at startup)

`mes.db` is wiped by the data generator, so both tables must be (re)created idempotently every time the bridge starts.

```sql
CREATE TABLE IF NOT EXISTS VisionDetections (
    DetectionID   INTEGER PRIMARY KEY AUTOINCREMENT,
    Timestamp     TEXT    NOT NULL,          -- UTC ISO-8601, from payload
    MachineID     INTEGER NOT NULL,          -- FK Machines.MachineID
    OrderID       INTEGER,                   -- FK WorkOrders.OrderID, NULL if no active order
    DefectType    TEXT    NOT NULL,          -- "class" from payload
    Confidence    REAL    NOT NULL,          -- 0.0–1.0
    BBox          TEXT,                      -- JSON string of the bbox object
    ImageName     TEXT,
    InferenceTimeMs REAL
);
-- One row per detection (an image with 3 boxes = 3 rows). Every detection is
-- saved regardless of confidence; the 0.80 gate only controls batching.

CREATE TABLE IF NOT EXISTS AgentAlerts (
    AlertID        INTEGER PRIMARY KEY AUTOINCREMENT,
    CreatedAt      TEXT    NOT NULL,         -- UTC ISO-8601
    MachineID      INTEGER NOT NULL,
    OrderID        INTEGER,
    DefectType     TEXT    NOT NULL,         -- dominant class in the batch
    DetectionCount INTEGER NOT NULL,
    WindowStart    TEXT    NOT NULL,
    WindowEnd      TEXT    NOT NULL,
    Status         TEXT    NOT NULL DEFAULT 'pending',
                   -- 'pending' | 'analyzing' | 'done' | 'failed'
    Report         TEXT,                     -- agent's markdown report, NULL until done
    CompletedAt    TEXT
);
```

**Ownership:** the bridge writes only `VisionDetections`. The agent side (`analyze_batch`) is the only writer of `AgentAlerts`. The dashboard only reads.

## 4. Lookup helpers (B) — module `bridge/mes_lookups.py`

```python
def get_frame_machines() -> list[dict]:
    # SELECT MachineID, Name, Status, WorkCenterID FROM Machines
    # WHERE Type = 'Frame Welding'
    # Returns [] if none. Queried live, never cached across DB regenerations.

def get_active_work_order(machine_id: int) -> dict | None:
    # SELECT OrderID, ProductID, LotNumber, Status, ActualStartTime
    # FROM WorkOrders WHERE MachineID = ? AND Status = 'in_progress'
    # ORDER BY ActualStartTime DESC LIMIT 1
    # Returns None if the machine has no active order (bridge then stores OrderID NULL).
```

The camera story: it watches the **Frame Welding** line (`Machines.Type = 'Frame Welding'`, models W-1000/W-2000/W-3000). The simulator picks its `machine_id` from `get_frame_machines()` at startup.

## 5. Bridge behavior (B)

- Save **every** incoming detection to `VisionDetections` immediately.
- **Confidence gate:** only detections with `confidence >= 0.80` count toward batching.
- **Batching:** first gated detection for a machine opens a 30-second window; when it closes, all gated detections collected in it form **one batch** and one call to `analyze_batch`. Constants: `CONF_GATE = 0.80`, `BATCH_WINDOW_SECONDS = 30`.
- **Resource bounds:** at most 32 machine windows and 500 gated detections per
  window are retained in memory. Extra gated detections remain persisted but
  do not start additional timers or agent work.

## 6. The seam — `analyze_batch` (A implements, B calls)

```python
def analyze_batch(batch: dict) -> int:
    """Called by the bridge once per closed batch window.
    Immediately inserts a 'pending' row into AgentAlerts and RETURNS the
    AlertID without waiting for the LLM (must return in <1s; the ~60s agent
    run happens in a background thread). Never raises — failures mark the
    alert 'failed'."""
```

`batch` shape (built by the bridge):

```python
{
    "machine_id": 12,
    "order_id": 4471,                  # or None
    "window_start": "2026-07-19T14:02:00Z",
    "window_end":   "2026-07-19T14:02:30Z",
    "detections": [                    # gated (>=0.80) only, chronological
        {
            "detection_id": 913,       # VisionDetections rowid, already saved
            "timestamp": "2026-07-19T14:02:31Z",
            "class": "scratches",
            "confidence": 0.8712,
            "image_name": "scratches_101.jpg"
        },
        ...
    ]
}
```

The agent API accepts only one cost-bearing run at a time and, by default, at
most 10 starts per rolling hour (`MES_MAX_RUNS_PER_HOUR`, bounded to 1–100).
Limit exhaustion returns `429` with `Retry-After`; concurrent starts return
`409`; failures are recorded as `failed`.

## 7. Change protocol

Anything in this file is an interface between A's and B's code. To change one: say it in chat, update this file in the same commit as the code change.
