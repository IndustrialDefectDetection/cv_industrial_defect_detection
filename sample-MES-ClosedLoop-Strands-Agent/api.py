"""FastAPI backend for the MES agent system (port 8000).

Endpoints (full contract with examples: TRACE_API.md):
  POST /analysis       run the structured, traced defect-analysis workflow
  POST /investigate    root-cause a camera-flagged defect burst (the bridge)
  POST /cancel         stop the in-flight run at its next checkpoint
  POST /chat/          run a traced supervisor-agent chat turn
  GET  /trace?since=N  live "under the hood" event stream (poll this)
  GET  /health         config/readiness report — first stop when debugging

The agent manager is built at startup, not import, so the server always boots
and /health can explain what is broken (missing API key, missing mes.db, ...)
instead of uvicorn dying with a stack trace before the port even opens.
"""

import asyncio
import logging
import os
import sys
import threading
import warnings
from contextlib import asynccontextmanager

# Windows: use selector-based event loops, not the default proactor.
#
# The Strands SDK runs every agent call through asyncio.run() on a worker
# thread (strands/_async.py: run_async), building and tearing down a fresh
# event loop each time. On Windows that default loop is ProactorEventLoop,
# whose close() can block indefinitely in _poll() waiting on overlapped I/O
# that never settles. A live stack dump of a wedged run caught exactly that:
#
#     result (concurrent/futures/_base.py:445)   <- agent call, waiting
#     ...
#     close (asyncio/windows_events.py:865)      <- loop teardown, stuck
#     _poll (asyncio/windows_events.py:775)
#
# The agents' work had finished; only the teardown hung, so the run never
# returned and never produced its report. SelectorEventLoop has no
# overlapped-I/O teardown path and does not exhibit this.
if sys.platform == "win32":
    with warnings.catch_warnings():
        # Deprecated in 3.14 and slated for removal in 3.16. There is no
        # global replacement yet (the successor is per-call
        # asyncio.Runner(loop_factory=...), which we do not control inside
        # the SDK), so revisit this when moving to 3.16.
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent_tracer import AgentTracer
from strands_agent import REPORTS_DIR, MESAgentManager

# One tracer for the process. It exists even when the manager fails to build,
# so /trace always answers. MESAgentManager shares this instance.
tracer = AgentTracer()

_manager: MESAgentManager | None = None
_manager_error: str | None = None

# One traced run at a time: the tracer holds a single run's events, and the
# supervisor Agent keeps conversation state, so concurrent runs would corrupt
# both. A second /chat/ while one is in flight gets a 409.
_run_guard = threading.Lock()


class _QuietPollingFilter(logging.Filter):
    """Hide access-log lines for the endpoints the dashboard polls.

    The trace dashboard hits /trace roughly once a second for the whole of a
    run, so without this the console is nothing but 200 OKs and the agents'
    own streamed output is impossible to read. Failures still get through:
    only 2xx/3xx responses are hidden.
    """

    _POLLED = ("GET /trace", "GET /health", "GET /defect-types")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if not any(path in message for path in self._POLLED):
            return True
        status = getattr(record, "args", None)
        code = status[-1] if isinstance(status, tuple) and status else None
        return not (isinstance(code, int) and 200 <= code < 400)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _manager, _manager_error
    logging.getLogger("uvicorn.access").addFilter(_QuietPollingFilter())
    try:
        _manager = MESAgentManager(tracer=tracer)
    except Exception as e:
        _manager_error = f"{type(e).__name__}: {e}"
    yield


app = FastAPI(lifespan=lifespan)

# Allows the Next.js frontend to call this API. Update origins if needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class ChatRequest(BaseModel):
    user_input: str


class AnalysisRequest(BaseModel):
    """Structured defect-analysis run — what the dashboard controls collect."""

    defect_type: str
    days_back: int = 7
    include_oee: bool = False
    include_downtime: bool = False
    include_changeover: bool = False
    include_maintenance: bool = True


@app.get("/health")
def health():
    """Always answers 200; the body says whether the agent side is usable."""
    if _manager is not None:
        model_id = _manager.model_id
        db_path = _manager.db_path
    else:
        model_id = os.getenv("MES_MODEL_ID", "claude-haiku-4-5-20251001")
        db_path = os.getenv("MES_DB_PATH") or os.path.join(os.path.abspath(""), "mes.db")
    return {
        "status": "ok",
        "model_id": model_id,
        "db_path": db_path,
        "db_exists": os.path.exists(db_path),
        "anthropic_api_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "agent_manager_ready": _manager is not None,
        "agent_manager_error": _manager_error,
    }


@app.get("/trace")
def trace(since: int = 0):
    """Events with seq > `since`, plus run status and current agent/tool.

    Clients should treat a returned `seq` lower than their cursor as "the
    buffer was reset" (new run or server restart) and start over from 0.
    """
    return tracer.snapshot(since)


@app.get("/defect-types")
def defect_types(days_back: int = 365):
    """Distinct defect types seen in the last `days_back` days — feeds UI dropdowns."""
    if _manager is None:
        raise HTTPException(status_code=503, detail=f"Agent manager not ready: {_manager_error}")
    result = _manager.get_defect_types(days_back)
    rows = (result or {}).get("rows") or []
    return {"defect_types": [r["DefectType"] for r in rows if r.get("DefectType")]}


@app.get("/report/{filename}")
def get_report(filename: str):
    """Serve a generated PDF so the dashboard stays a pure HTTP client."""
    # Basename only, and the resolved path must stay inside REPORTS_DIR:
    # the filename reaches us from a client, so '../' must not escape.
    safe_name = os.path.basename(filename)
    if not safe_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf reports are served")
    path = (REPORTS_DIR / safe_name).resolve()
    if not str(path).startswith(str(REPORTS_DIR.resolve())) or not path.is_file():
        raise HTTPException(status_code=404, detail=f"No such report: {safe_name}")
    return FileResponse(path, media_type="application/pdf", filename=safe_name)


class BurstRequest(BaseModel):
    """A camera-flagged defect burst, as the bridge's analyze_batch sends it."""

    machine_id: int
    defect_type: str
    detection_count: int
    window_start: str
    window_end: str
    order_id: int | None = None
    detections: list[dict] = []


@app.post("/investigate")
def investigate(request: BurstRequest):
    """Root-cause a defect burst detected by the CV pipeline (CONTRACTS.md §6).

    Called by the bridge over HTTP so the agent stack stays in this project -
    one toolchain, one set of credentials - and every investigation shows up
    in the live trace dashboard like any other run.
    """
    if _manager is None:
        raise HTTPException(status_code=503, detail=f"Agent manager not ready: {_manager_error}")
    if not _run_guard.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A run is already in progress; wait for it to finish.")
    try:
        return _manager.investigate_detection_burst(
            machine_id=request.machine_id,
            defect_type=request.defect_type,
            detection_count=request.detection_count,
            window_start=request.window_start,
            window_end=request.window_end,
            order_id=request.order_id,
            detections=request.detections,
        )
    finally:
        _run_guard.release()


@app.post("/cancel")
def cancel_run():
    """Ask the in-flight run to stop at its next checkpoint.

    Returns immediately; cancellation is cooperative, so the run unwinds
    within seconds (at its next subagent delegation or database query) and
    the original /analysis call returns with status 'cancelled'.
    """
    if _manager is None:
        raise HTTPException(status_code=503, detail=f"Agent manager not ready: {_manager_error}")
    running = _run_guard.locked()
    _manager.cancel()
    return {"cancelling": running,
            "detail": "Cancelling the run" if running else "No run was in progress"}


@app.post("/analysis")
def run_analysis(request: AnalysisRequest):
    """Run the structured defect-analysis workflow.

    Preferred over /chat/ for dashboard-driven runs: the scope flags reach
    the supervisor as parameters instead of being described in prose, and
    run_defect_analysis handles its own tracer reset/start/end.
    """
    if _manager is None:
        raise HTTPException(status_code=503, detail=f"Agent manager not ready: {_manager_error}")
    if not _run_guard.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A run is already in progress; wait for it to finish.")
    try:
        return _manager.run_defect_analysis(
            defect_type=request.defect_type,
            days_back=request.days_back,
            include_oee=request.include_oee,
            include_downtime=request.include_downtime,
            include_changeover=request.include_changeover,
            include_maintenance=request.include_maintenance,
        )
    finally:
        _run_guard.release()


@app.post("/chat/")
def send_message(message: ChatRequest):
    if _manager is None:
        raise HTTPException(status_code=503, detail=f"Agent manager not ready: {_manager_error}")
    if not _run_guard.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A run is already in progress; wait for it to finish.")
    try:
        query = message.user_input
        tracer.reset()
        tracer.run_start(f"Chat: {query[:80]}", params={"user_input": query[:200]})
        try:
            chat_agent = _manager.get_conversational_agent()
            response = chat_agent(query)
            analysis_text = response.message["content"][0]["text"]
        except Exception as e:
            tracer.error(None, f"{type(e).__name__}: {e}")
            tracer.run_end("failed", error=str(e))
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
        tracer.run_end("completed")
        return {"analysis": analysis_text}
    finally:
        _run_guard.release()
