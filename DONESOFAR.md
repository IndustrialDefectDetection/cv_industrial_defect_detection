## What is the Bridge?

The Bridge is a **FastAPI service on port 8081** that sits between the **CV inference API** (steel defect detection, port 8080) and the **MES database** (PostgreSQL). It's the glue that turns individual defect detection results into batched manufacturing alerts that the agent system can analyze.

```
┌──────────────┐     POST /predict      ┌──────────────────┐     POST /detection     ┌───────────────┐
│  Inference    │ ◄────── images ─────── │    Simulator     │ ──────── §2 payload ────► │   Bridge      │
│  API          │ ────── detections ──► │  (or curl test)  │                         │  (port 8081)  │
│  (port 8080)  │                        └──────────────────┘                         └───────┬───────┘
│ steel-defect- │                                                                           │
│ detection     │                                                                           ▼
└──────────────┘                                                                  ┌──────────────────┐
                                                                                  │   PostgreSQL     │
                                                                                  │  mescopy_v1 DB   │
                                                                                  │                  │
                                                                                  │ • Machines       │
                                                                                  │ • WorkOrders     │
                                                                                  │ • VisionDetections│
                                                                                  │ • AgentAlerts    │
                                                                                  └──────────────────┘
```

## All Files Implemented

All under `industrial-data-store-simulation-chatbot/bridge/`:

### 1. `db_config.py` — Shared Configuration
- PostgreSQL connection parameters (host, port, user, password, database)
- Constants: `CONF_GATE = 0.80`, `BATCH_WINDOW_SECONDS = 30`
- All configurable via environment variables (`MES_PG_USER`, `MES_PG_HOST`, etc.)

### 2. `mes_lookups.py` — CONTRACTS.md §4 Lookup Helpers
Two functions that query the MES database live (never cached):

| Function | What it does |
|----------|-------------|
| `get_frame_machines()` | Returns all Frame Welding machines (MachineID, Name, Status, WorkCenterID) — the cameras watch this line |
| `get_active_work_order(machine_id)` | Returns the in-progress work order for a machine, or None if no active order |

### 3. `batch_manager.py` — CONTRACTS.md §5 Batching Logic
- **Per-machine batch windows**: When the first detection with confidence ≥ 0.80 arrives for a machine, a 30-second timer starts
- **Window behavior**: All gated detections within that 30s window are collected together
- **Window close**: When timer expires, builds a §6 batch dict and calls `analyze_batch()` in a background thread
- **Thread-safe**: Uses `threading.Lock()` for concurrent access

### 4. `analyze_batch.py` — CONTRACTS.md §6 The Seam
- Single function `analyze_batch(batch)` that:
  1. Determines the **dominant defect class** (most frequent in the batch)
  2. Inserts a **`pending` row** into `AgentAlerts` table
  3. **Returns the AlertID** in < 1 second
- Never raises exceptions — failures just log an error
- The actual LLM analysis (the slow 60s part) happens in a separate thread later

### 5. `bridge.py` — CONTRACTS.md §5 FastAPI Bridge (Main File)
The web service itself with two endpoints:

**`GET /health`** — Simple health check, also verifies PostgreSQL connectivity

**`POST /detection`** — The core endpoint. Receives a payload like:
```json
{
  "timestamp": "2026-07-24T11:30:00Z",
  "machine_id": 1,
  "image_name": "scratches_101.jpg",
  "inference_time_ms": 42.5,
  "detections": [
    {"class_": "scratches", "class_id": 5, "confidence": 0.87, "bbox": {...}},
    {"class_": "inclusion", "class_id": 1, "confidence": 0.92, "bbox": {...}},
    {"class_": "scratches", "class_id": 5, "confidence": 0.35, "bbox": {...}}
  ]
}
```

For each detection, the bridge does:
1. **Look up active work order** for the machine (via `mes_lookups.get_active_work_order`)
2. **Save to VisionDetections** immediately (all detections, regardless of confidence)
3. **Apply confidence gate** — only detections ≥ 0.80 are fed to `batch_manager`
4. **Return** the saved detection IDs and order ID

### 6. `simulator.py` — CONTRACTS.md §2 Simulator (Optional)
A script that automates the full pipeline:
1. Picks a random Frame Welding machine from the DB
2. Reads images from a directory
3. POSTs each image to the inference API (port 8080)
4. Wraps the response in the §2 payload format
5. POSTs to the bridge (port 8081)
6. Can loop continuously with configurable interval

### 7. Modified: `sample-MES-ClosedLoop-Strands-Agent/setupdatabase.py`
Added `create_contract_tables()` function that creates the `VisionDetections` and `AgentAlerts` tables in PostgreSQL when you run the setup script.

## Data Flow Summary

```
1. Camera captures image
2. POST to inference API at port 8080 → returns detections
3. Bridge receives payload at POST /detection
4. Bridge looks up active work order for that machine
5. Bridge saves ALL detections to VisionDetections table
6. For detections ≥ 0.80 confidence, feeds to BatchManager
7. BatchManager opens a 30-second window per machine
8. When window closes, calls analyze_batch()
9. analyze_batch() inserts a 'pending' row into AgentAlerts
10. Agent system reads AgentAlerts later, does LLM analysis, updates status to 'done'
```

## What Was Verified in the Test Run
All steps 1-9 work without any LLM/Claude involvement:
- ✅ Bridge starts and connects to PostgreSQL
- ✅ Detection payload accepted at POST /detection
- ✅ Active work order resolved (order_id=4901)
- ✅ 3 detections saved to VisionDetections
- ✅ 2 of 3 passed the confidence gate (0.87 and 0.92)
- ✅ BatchManager opened a window for machine 1
- ✅ After 30 seconds, AgentAlert created with status 'pending'