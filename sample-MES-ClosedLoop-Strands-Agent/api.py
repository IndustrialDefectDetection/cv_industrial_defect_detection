"""FastAPI backend for the MES agent system (port 8000).

Endpoints (full contract with examples: TRACE_API.md):
  POST /analysis       run the structured, traced defect-analysis workflow
  POST /investigate    root-cause a camera-flagged defect burst (the bridge)
  POST /cancel         stop all active agents in the in-flight run
  POST /chat/          run a traced supervisor-agent chat turn
  GET  /trace?since=N  live "under the hood" event stream (poll this)
  GET  /health         config/readiness report — first stop when debugging

The agent manager is built at startup, not import, so the server always boots
and /health can explain what is broken (missing API key, missing mes.db, ...)
instead of uvicorn dying with a stack trace before the port even opens.
"""

import asyncio
import json
import logging
import os
import queue
import re
import secrets
import sys
import threading
import time
import warnings
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Callable, Literal
from uuid import UUID

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

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
)

from agent_tracer import AgentTracer
from display_security import safe_log_text
from report_paths import InvalidReportPath, resolve_existing_report
from strands_agent import MESAgentManager, RunCancelled

# One tracer for the process. It exists even when the manager fails to build,
# so /trace always answers. MESAgentManager shares this instance.
tracer = AgentTracer()

_manager: MESAgentManager | None = None
_manager_error: str | None = None

# One traced run at a time: the tracer holds a single run's events, and the
# supervisor Agent keeps conversation state, so concurrent runs would corrupt
# both. A second /chat/ while one is in flight gets a 409.
_run_guard = threading.Lock()
_RUN_OWNER_LOCK = threading.Lock()
_active_run_owner: str | None = None
_trace_owner: str | None = None
_CHAT_HEARTBEAT_SECONDS = 10
_RUN_BUDGET_LOCK = threading.Lock()
_RUN_STARTS: deque[float] = deque()
_RUN_BUDGET_WINDOW_SECONDS = 60 * 60
_MAX_CHAT_CONTEXT_MESSAGES = 20
_MAX_CHAT_CONTEXT_BYTES = 64 * 1024


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError:
        logging.getLogger(__name__).warning(
            "Ignoring invalid %s value; using %s",
            name,
            default,
        )
        value = default
    return max(minimum, min(value, maximum))


_MAX_RUNS_PER_HOUR = _bounded_int_env(
    "MES_MAX_RUNS_PER_HOUR",
    default=10,
    minimum=1,
    maximum=100,
)
DefectType = Literal[
    "crazing",
    "inclusion",
    "patches",
    "pitted_surface",
    "rolled-in_scale",
    "scratches",
]
SAFE_IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def require_internal_api_token(
    supplied_token: Annotated[
        str | None,
        Header(alias="X-MES-Internal-Token"),
    ] = None,
) -> None:
    """Authenticate server-to-server callers without exposing user sessions.

    The browser never receives this token. Next.js validates the user's Better
    Auth session, then adds the token while proxying to this loopback service.
    The bridge and trace viewer use the same header.
    """
    expected_token = os.getenv("MES_INTERNAL_API_TOKEN", "")
    if len(expected_token) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal API authentication is not configured",
        )
    if supplied_token is None or not secrets.compare_digest(
        supplied_token,
        expected_token,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


INTERNAL_API_AUTH = [Depends(require_internal_api_token)]


class InternalBoundaryMiddleware:
    """Authenticate and count bytes before FastAPI parses protected bodies."""

    _PUBLIC_PATHS = {"/health"}

    def __init__(self, app, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def _respond(self, scope, receive, send, code: int, detail: str):
        await JSONResponse(
            status_code=code,
            content={"detail": detail},
            headers={"Cache-Control": "no-store"},
        )(scope, receive, send)

    async def _send_protected_response(self, message, send):
        """Prevent authenticated MES data from entering intermediary caches."""
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            header_names = {name.lower() for name, _value in headers}
            if b"cache-control" not in header_names:
                headers.append((b"cache-control", b"private, no-store"))
            if b"x-content-type-options" not in header_names:
                headers.append((b"x-content-type-options", b"nosniff"))
            if b"referrer-policy" not in header_names:
                headers.append((b"referrer-policy", b"no-referrer"))
            message = {**message, "headers": headers}
        await send(message)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if path in self._PUBLIC_PATHS:
            if method not in {"GET", "HEAD"}:
                await self._respond(scope, receive, send, 405, "Method not allowed")
                return
            await self.app(scope, receive, send)
            return
        else:
            configured = os.getenv("MES_INTERNAL_API_TOKEN", "")
            supplied = headers.get(b"x-mes-internal-token")
            if len(configured) < 32:
                await self._respond(
                    scope,
                    receive,
                    send,
                    503,
                    "Internal API authentication is not configured",
                )
                return
            if supplied is None or not secrets.compare_digest(
                supplied, configured.encode("utf-8")
            ):
                await self._respond(scope, receive, send, 401, "Unauthorized")
                return

        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                declared_length = int(declared)
            except ValueError:
                await self._respond(
                    scope, receive, send, 400, "Invalid Content-Length"
                )
                return
            if declared_length < 0:
                await self._respond(
                    scope, receive, send, 400, "Invalid Content-Length"
                )
                return
            if declared_length > self.max_body_bytes:
                await self._respond(
                    scope, receive, send, 413, "Request body is too large"
                )
                return

        buffered_messages = []
        received = 0
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self._respond(
                    scope, receive, send, 408, "Request body timed out"
                )
                return
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except asyncio.TimeoutError:
                await self._respond(
                    scope, receive, send, 408, "Request body timed out"
                )
                return
            buffered_messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > self.max_body_bytes:
                await self._respond(
                    scope, receive, send, 413, "Request body is too large"
                )
                return
            if not message.get("more_body", False):
                break

        next_message = 0

        async def replay_receive():
            nonlocal next_message
            if next_message < len(buffered_messages):
                message = buffered_messages[next_message]
                next_message += 1
                return message
            # StreamingResponse waits here for a real client disconnect after
            # the buffered request body has been replayed.
            return await receive()

        async def protected_send(message):
            await self._send_protected_response(message, send)

        await self.app(scope, replay_receive, protected_send)


def _manager_not_ready() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Agent backend is not ready",
    )


def _reserve_run_budget() -> None:
    """Enforce a process-wide cap on cost-bearing agent runs."""
    now = time.monotonic()
    cutoff = now - _RUN_BUDGET_WINDOW_SECONDS
    with _RUN_BUDGET_LOCK:
        while _RUN_STARTS and _RUN_STARTS[0] <= cutoff:
            _RUN_STARTS.popleft()
        if len(_RUN_STARTS) >= _MAX_RUNS_PER_HOUR:
            retry_after = max(
                1,
                int(_RUN_BUDGET_WINDOW_SECONDS - (now - _RUN_STARTS[0])),
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Hourly analysis limit reached",
                headers={"Retry-After": str(retry_after)},
            )
        _RUN_STARTS.append(now)


def _acquire_run_slot(
    owner: str = "service",
    on_acquired: Callable[[], None] | None = None,
) -> None:
    global _active_run_owner, _trace_owner
    with _RUN_OWNER_LOCK:
        if not _run_guard.acquire(blocking=False):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A run is already in progress; wait for it to finish.",
            )
        try:
            _reserve_run_budget()
            # Resetting the trace and publishing its new owner are one atomic
            # transition relative to trace readers and cancellation requests.
            tracer.reset()
            _active_run_owner = owner
            _trace_owner = owner
            if on_acquired is not None:
                on_acquired()
        except BaseException:
            _active_run_owner = None
            _trace_owner = None
            _run_guard.release()
            raise


def _release_run_slot() -> None:
    global _active_run_owner
    with _RUN_OWNER_LOCK:
        _active_run_owner = None
        _run_guard.release()


def _user_owner(user_id: str) -> str:
    return f"user:{user_id}"


def _owner_allows_user(owner: str | None, user_id: str | None) -> bool:
    """Service callers omit a user ID; browser proxies always include one."""
    if user_id is None:
        return True
    return owner is None or owner == _user_owner(user_id)


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
        logging.getLogger(__name__).error(
            "Agent manager startup failed: %s",
            safe_log_text(e),
        )
    yield


app = FastAPI(
    lifespan=lifespan,
    # Operational schemas are not a public browser surface. Health is the only
    # unauthenticated endpoint in the repository contract.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(InternalBoundaryMiddleware, max_body_bytes=1024 * 1024)


class ChatHistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=_MAX_CHAT_CONTEXT_BYTES)

    @field_validator("content")
    @classmethod
    def content_cannot_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("history content cannot be blank")
        return value


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    user_input: str = Field(min_length=1, max_length=4_000)
    history: list[ChatHistoryMessage] = Field(
        max_length=_MAX_CHAT_CONTEXT_MESSAGES,
    )

    @field_validator("history")
    @classmethod
    def history_is_bounded_and_complete(
        cls,
        value: list[ChatHistoryMessage],
    ) -> list[ChatHistoryMessage]:
        for index, message in enumerate(value):
            expected_role = "user" if index % 2 == 0 else "assistant"
            if message.role != expected_role:
                raise ValueError(
                    "history must contain complete user/assistant turns"
                )
        if len(value) % 2 != 0:
            raise ValueError(
                "history must contain complete user/assistant turns"
            )
        if sum(len(message.content.encode("utf-8")) for message in value) > (
            _MAX_CHAT_CONTEXT_BYTES
        ):
            raise ValueError("history is too large")
        return value


class AnalysisRequest(BaseModel):
    """Structured defect-analysis run — what the dashboard controls collect."""

    model_config = ConfigDict(extra="forbid")

    defect_type: str = Field(min_length=1, max_length=100)
    days_back: int = Field(default=7, ge=1, le=3_650)
    include_oee: bool = False
    include_downtime: bool = False
    include_changeover: bool = False
    include_maintenance: bool = True

    @field_validator("defect_type")
    @classmethod
    def defect_type_has_no_control_characters(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(ord(character) < 32 for character in cleaned):
            raise ValueError("defect_type contains invalid characters")
        return cleaned


@app.get("/health", include_in_schema=False)
def health():
    """Minimal readiness response; implementation details stay in server logs."""
    ready = (
        _manager is not None
        and len(os.getenv("MES_INTERNAL_API_TOKEN", "")) >= 32
    )
    return JSONResponse(
        {
            "status": "ok" if ready else "unavailable",
            "agent_manager_ready": ready,
        },
        status_code=200 if ready else 503,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


UserIdHeader = Annotated[
    str | None,
    Header(
        alias="X-MES-User-ID",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]


@app.get("/trace", dependencies=INTERNAL_API_AUTH)
def trace(
    since: Annotated[int, Query(ge=0)] = 0,
    user_id: UserIdHeader = None,
):
    """Events with seq > `since`, plus run status and current agent/tool.

    Clients should treat a returned `seq` lower than their cursor as "the
    buffer was reset" (new run or server restart) and start over from 0.
    """
    with _RUN_OWNER_LOCK:
        if not _owner_allows_user(_trace_owner, user_id):
            raise HTTPException(status_code=403, detail="Trace is not available")
        return tracer.snapshot(since)


@app.get("/defect-types", dependencies=INTERNAL_API_AUTH)
def defect_types(days_back: Annotated[int, Query(ge=1, le=3_650)] = 365):
    """Distinct defect types seen in the last `days_back` days — feeds UI dropdowns."""
    if _manager is None:
        raise _manager_not_ready()
    result = _manager.get_defect_types(days_back)
    rows = (result or {}).get("rows") or []
    return {"defect_types": [r["DefectType"] for r in rows if r.get("DefectType")]}


@app.get("/alerts", dependencies=INTERNAL_API_AUTH)
def alerts(limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """Alerts raised by the CV pipeline, newest first (CONTRACTS.md §3).

    Exists so the dashboard stays a pure HTTP client rather than opening its
    own database connection. Cheap enough to poll: a plain SELECT, no model
    call, and it does not touch the one-run-at-a-time guard - reading the
    alert list must keep working while an investigation is in flight, which
    is exactly when someone is watching it.
    """
    if _manager is None:
        raise _manager_not_ready()
    return _manager.get_recent_alerts(limit)


@app.get("/report/{filename}", dependencies=INTERNAL_API_AUTH)
def get_report(filename: str):
    """Serve a generated PDF so the dashboard stays a pure HTTP client."""
    try:
        path = resolve_existing_report(filename)
    except InvalidReportPath as exc:
        raise HTTPException(status_code=404, detail="Report not found") from exc
    return FileResponse(path, media_type="application/pdf", filename=path.name)


class BurstDetection(BaseModel):
    """One already-persisted, confidence-gated bridge detection."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    detection_id: StrictInt = Field(ge=1)
    timestamp: datetime
    class_: DefectType = Field(alias="class")
    confidence: StrictFloat = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    image_name: str = Field(min_length=1, max_length=255)

    @field_validator("timestamp")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @field_validator("image_name")
    @classmethod
    def image_name_is_plain(cls, value: str) -> str:
        if Path(value).name != value or not SAFE_IMAGE_NAME.fullmatch(value):
            raise ValueError("image_name contains unsupported characters")
        return value


class BurstRequest(BaseModel):
    """A camera-flagged defect burst, as the bridge's analyze_batch sends it."""

    model_config = ConfigDict(extra="forbid")

    machine_id: StrictInt = Field(ge=1)
    defect_type: DefectType
    detection_count: StrictInt = Field(ge=1, le=500)
    window_start: datetime
    window_end: datetime
    order_id: StrictInt | None = Field(default=None, ge=1)
    detections: list[BurstDetection] = Field(
        min_length=1,
        max_length=500,
    )

    @field_validator("window_start", "window_end")
    @classmethod
    def windows_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("window timestamps must include a timezone")
        return value


@app.post("/investigate", dependencies=INTERNAL_API_AUTH)
def investigate(request: BurstRequest):
    """Root-cause a defect burst detected by the CV pipeline (CONTRACTS.md §6).

    Called by the bridge over HTTP so the agent stack stays in this project -
    one toolchain, one set of credentials - and every investigation shows up
    in the live trace dashboard like any other run.
    """
    if _manager is None:
        raise _manager_not_ready()
    if request.detection_count != len(request.detections):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="detection_count must match detections",
        )
    if request.defect_type not in {
        detection.class_ for detection in request.detections
    }:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="defect_type must occur in detections",
        )
    if request.window_end < request.window_start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="window_end must not precede window_start",
        )
    if request.window_end - request.window_start > timedelta(minutes=5):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Burst window is too large",
        )
    _acquire_run_slot()
    try:
        return _manager.investigate_detection_burst(
            machine_id=request.machine_id,
            defect_type=request.defect_type,
            detection_count=request.detection_count,
            window_start=request.window_start.isoformat(),
            window_end=request.window_end.isoformat(),
            order_id=request.order_id,
            detections=[
                detection.model_dump(by_alias=True, mode="json")
                for detection in request.detections
            ],
        )
    finally:
        _release_run_slot()


@app.post("/cancel", dependencies=INTERNAL_API_AUTH)
def cancel_run(user_id: UserIdHeader = None):
    """Ask the in-flight run to stop at its next checkpoint.

    Returns immediately. The manager signals every active nested agent and
    prevents later phases or retries from starting. Strands cancellation is
    cooperative, so an already-running Python tool or a model request that has
    not started streaming may still wait for its configured timeout. The
    original streaming request ends with status 'cancelled' after it unwinds.
    """
    if _manager is None:
        raise _manager_not_ready()
    with _RUN_OWNER_LOCK:
        running = _run_guard.locked()
        if running and not _owner_allows_user(_active_run_owner, user_id):
            raise HTTPException(status_code=403, detail="Run is not available")
        if running:
            # Keep ownership stable until the cancellation signal reaches the
            # manager. A different owner's run cannot start in this window.
            _manager.cancel()
    return {"cancelling": running,
            "detail": "Cancelling the run" if running else "No run was in progress"}


@app.post("/analysis", dependencies=INTERNAL_API_AUTH)
def run_analysis(request: AnalysisRequest):
    """Run the structured defect-analysis workflow.

    Preferred over /chat/ for dashboard-driven runs: the scope flags reach
    the supervisor as parameters instead of being described in prose, and
    run_defect_analysis handles its own tracer reset/start/end.
    """
    if _manager is None:
        raise _manager_not_ready()
    available = _manager.get_defect_types(3_650)
    known_types = {
        row["DefectType"]
        for row in (available or {}).get("rows", [])
        if row.get("DefectType")
    }
    if request.defect_type not in known_types:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown defect_type",
        )
    _acquire_run_slot()
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
        _release_run_slot()


@app.post("/chat/", dependencies=INTERNAL_API_AUTH)
def send_message(message: ChatRequest, user_id: UserIdHeader = None):
    if _manager is None:
        raise _manager_not_ready()
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-MES-User-ID is required",
        )
    query = message.user_input.strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_input cannot be blank",
        )
    owner = _user_owner(user_id)
    _acquire_run_slot(
        owner,
        on_acquired=_manager.prepare_chat_request,
    )

    terminal_events: queue.Queue[dict] = queue.Queue(maxsize=1)
    worker = threading.Thread(
        target=_run_chat_worker,
        args=(
            _manager,
            query,
            str(message.conversation_id),
            [history.model_dump() for history in message.history],
            terminal_events,
        ),
        name="mes-chat-run",
        daemon=True,
    )
    try:
        worker.start()
    except Exception:
        _release_run_slot()
        raise

    return StreamingResponse(
        _iter_chat_events(_manager, worker, terminal_events, owner),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "private, no-store, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _run_chat_worker(
    manager,
    query: str,
    conversation_id: str,
    history: list[dict[str, str]],
    terminal_events: queue.Queue[dict],
):
    """Run the blocking agent stack while the response stream stays alive."""
    terminal_event: dict
    try:
        tracer.run_start(
            f"Chat: {query[:80]}",
            params={
                "conversation_id": conversation_id,
                "user_input": query[:200],
            },
        )
        response = manager.run_chat(query, history)
        if response.stop_reason == "cancelled":
            tracer.run_end("cancelled")
            terminal_event = {
                "type": "result",
                "data": {"status": "cancelled"},
            }
        else:
            analysis_text = response.message["content"][0]["text"]
            tracer.run_end("completed")
            terminal_event = {
                "type": "result",
                "data": {"analysis": analysis_text},
            }
    except RunCancelled:
        tracer.run_end("cancelled")
        terminal_event = {
            "type": "result",
            "data": {"status": "cancelled"},
        }
    except Exception as e:
        logging.getLogger(__name__).error(
            "Chat agent run failed: %s",
            safe_log_text(e),
        )
        tracer.error(None, f"Chat agent run failed ({type(e).__name__})")
        tracer.run_end("failed", error="Agent run failed")
        terminal_event = {
            "type": "error",
            "error": "The analysis failed",
        }
    finally:
        # The client must not receive a terminal event while the API would
        # still reject its next request as busy.
        _release_run_slot()
    terminal_events.put(terminal_event)


def _iter_chat_events(
    manager,
    worker: threading.Thread,
    terminal_events: queue.Queue[dict],
    originating_owner: str,
):
    """Emit an immediate acknowledgement, heartbeats, then one final event."""
    terminal_delivered = False
    try:
        yield json.dumps({"type": "started"}) + "\n"
        while True:
            try:
                event = terminal_events.get(timeout=_CHAT_HEARTBEAT_SECONDS)
            except queue.Empty:
                yield json.dumps({"type": "heartbeat"}) + "\n"
                continue

            terminal_delivered = True
            yield json.dumps(event, ensure_ascii=False) + "\n"
            return
    finally:
        # A browser/navigation disconnect must not leave an invisible run
        # holding the global guard. Cancellation remains cooperative.
        if not terminal_delivered and worker.is_alive():
            with _RUN_OWNER_LOCK:
                if (
                    _run_guard.locked()
                    and _active_run_owner == originating_owner
                ):
                    manager.cancel()
