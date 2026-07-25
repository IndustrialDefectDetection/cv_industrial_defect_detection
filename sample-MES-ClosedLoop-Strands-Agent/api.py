"""FastAPI backend for the MES agent system (port 8000).

Endpoints (full contract with examples: TRACE_API.md):
  POST /chat/          run a traced supervisor-agent chat turn
  GET  /trace?since=N  live "under the hood" event stream (poll this)
  GET  /health         config/readiness report — first stop when debugging

The agent manager is built at startup, not import, so the server always boots
and /health can explain what is broken (missing API key, missing mes.db, ...)
instead of uvicorn dying with a stack trace before the port even opens.
"""

import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent_tracer import AgentTracer
from strands_agent import MESAgentManager

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

    _POLLED = ("GET /trace", "GET /health")

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
            response = _manager.get_supervisor_agent()(query)
            analysis_text = response.message["content"][0]["text"]
        except Exception as e:
            tracer.error(None, f"{type(e).__name__}: {e}")
            tracer.run_end("failed", error=str(e))
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
        tracer.run_end("completed")
        return {"analysis": analysis_text}
    finally:
        _run_guard.release()
