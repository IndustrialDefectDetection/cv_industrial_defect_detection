"""
agent_tracer.py — a thread-safe, in-memory trace of the multi-agent workflow.

This is the single source of truth for "what is happening under the hood" while
the Supervisor agent orchestrates the Monitor / Analyzer / Planner / Verifier /
Executor sub-agents. It captures, per agent:

  * the text each agent streams ("what the agents are saying to each other" —
    the Supervisor's prompt to a sub-agent is a tool argument; the sub-agent's
    reply is streamed text),
  * every tool call with its arguments and its result,
  * the literal SQL each tool runs (surfaced from the one query chokepoint,
    `_execute_safe_query`), with row counts and timing,
  * agent/tool start-stop boundaries so a UI can show what is running *now*.

Design notes
------------
* Everything is a plain JSON-serialisable dict so the exact same event stream
  can later be pushed over SSE to the Next.js frontend without rework — the
  Streamlit view just polls `snapshot()` today.
* All mutation goes through a single re-entrant lock. The agent workflow runs in
  a background thread; the UI reads from the Streamlit thread. Neither touches
  the other's framework objects — only this tracer, which is safe to share.
* Events carry a monotonic `seq` so a consumer (Streamlit poll loop or an SSE
  bridge) can ask for "everything after seq N".

Attach it to Strands agents with `attach_tracer(agent, label, tracer)`.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)
from display_security import safe_terminal_text

# Event kinds emitted onto the stream. Kept as bare strings (not an Enum) so the
# serialised form is trivially JSON/SSE friendly.
RUN_START = "run_start"
RUN_END = "run_end"
AGENT_START = "agent_start"
AGENT_END = "agent_end"
TEXT = "text"
TOOL_START = "tool_start"
TOOL_END = "tool_end"
QUERY = "query"
ERROR = "error"

# How much of any single blob (result preview, streamed text, SQL) we keep. The
# full model report is returned through the normal analysis result; the trace is
# a live X-ray, not an archive.
_MAX_TEXT = 6000
_MAX_PREVIEW = 4000
_MAX_EVENTS = 5000
_MAX_COLLECTION_ITEMS = 50
_MAX_JSON_DEPTH = 6


# The (agent, tool_name, tool_use_id) executing on *this* logical thread of
# control. A ContextVar rather than a field on the tracer because the SDK runs
# an agent's tool calls concurrently: with a single shared field, whichever
# tool started last wins and every query gets stamped with its name (observed:
# five different Monitor queries all labelled "SQL via fetch_work_orders_context",
# and one orphaned into the agent-less "Run" group when a tool_end cleared the
# field first). ContextVars follow the SDK's hops between async tasks and
# worker threads, so each query reads back the tool that actually ran it.
_ACTIVE_TOOL: contextvars.ContextVar = contextvars.ContextVar(
    "mes_active_tool", default=(None, None, None)
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = safe_terminal_text(text, max_chars=limit + 1)
    if len(text) > limit:
        return text[:limit] + f"\n… (truncated, {len(text)} chars total)"
    return text


def _extract_tool_result(result: Any) -> dict:
    """Flatten a Strands ToolResult (or exception) into a small display dict.

    ToolResult is ``{"status", "toolUseId", "content": [{"text"|"json"|...}]}``.
    We pull out a human-readable preview plus, when the underlying tool returned
    one of this project's ``{"success", "rows", "row_count", ...}`` dicts, the
    row count — that is what makes the feed readable at a glance.
    """
    out: dict[str, Any] = {"status": None, "preview": "", "row_count": None}
    if result is None:
        return out

    # An exception was raised by the tool.
    if isinstance(result, BaseException):
        out["status"] = "error"
        out["preview"] = f"{type(result).__name__}: tool execution failed"
        return out

    if isinstance(result, dict):
        out["status"] = result.get("status")
        if str(out["status"]).lower() == "error":
            out["preview"] = "Tool execution failed"
            return out
        blocks = result.get("content") or []
        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            if "text" in block:
                parts.append(str(block["text"]))
            elif "json" in block:
                payload = block["json"]
                parts.append(_summarise_json_payload(payload, out))
            else:
                parts.append(f"<{', '.join(block.keys())}>")
        out["preview"] = _truncate("\n".join(parts).strip(), _MAX_PREVIEW)
        return out

    out["preview"] = _truncate(result, _MAX_PREVIEW)
    return out


def _summarise_json_payload(payload: Any, out: dict) -> str:
    """Pull a row count and a compact string out of a JSON tool payload."""
    if isinstance(payload, dict):
        if out.get("row_count") is None and "row_count" in payload:
            out["row_count"] = payload.get("row_count")
        # Drop the heavy/non-serialisable bits from this project's query dicts.
        compact = {
            k: v for k, v in payload.items() if k not in ("dataframe", "rows")
        }
        if "rows" in payload and isinstance(payload["rows"], list):
            compact["rows_sample"] = payload["rows"][:3]
        return _truncate(str(compact), _MAX_PREVIEW)
    return _truncate(str(payload), _MAX_PREVIEW)


class AgentTracer:
    """Thread-safe event buffer for one workflow run."""

    def __init__(self, max_events: int = _MAX_EVENTS) -> None:
        self._lock = threading.RLock()
        self._events: list[dict] = []
        self._seq = 0
        self._max_events = max_events
        # Per-agent accumulators for streamed text/reasoning deltas.
        self._text_buf: dict[str, str] = {}
        self._reason_buf: dict[str, str] = {}
        # toolUseId -> (start_time, agent, tool_name) for duration + result join.
        self._open_tools: dict[str, tuple[float, str, str]] = {}
        # The agent/tool currently executing, so `_execute_safe_query` can label
        # its SQL without every tool needing to thread context through by hand.
        self._current: tuple[Optional[str], Optional[str]] = (None, None)
        self._run: dict[str, Any] = {"status": "idle"}

    # ------------------------------------------------------------------ core
    def _emit(self, agent: Optional[str], kind: str, **data: Any) -> dict:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": time.time(),
                "iso": _now_iso(),
                "agent": agent,
                "kind": kind,
                **data,
            }
            self._events.append(event)
            if len(self._events) > self._max_events:
                # Drop oldest, keep the stream bounded for long runs.
                self._events = self._events[-self._max_events :]
            return event

    def reset(self) -> None:
        """Clear the buffer for a fresh run (call before each analysis)."""
        with self._lock:
            self._events.clear()
            self._seq = 0
            self._text_buf.clear()
            self._reason_buf.clear()
            self._open_tools.clear()
            self._current = (None, None)
            self._run = {"status": "idle"}

    def snapshot(self, since: int = 0) -> dict:
        """Return events with seq > `since`, plus run status and live activity."""
        with self._lock:
            events = [e for e in self._events if e["seq"] > since]
            return {
                "seq": self._seq,
                "events": events,
                "run": dict(self._run),
                "current": {"agent": self._current[0], "tool": self._current[1]},
            }

    # --------------------------------------------------------------- run edges
    def run_start(self, label: str, params: Optional[dict] = None) -> None:
        # A fresh id per run lets a polling client tell "new run replaced the
        # buffer" apart from "same run, more events" even after a seq reset.
        run_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._run = {
                "status": "running",
                "run_id": run_id,
                "label": label,
                "started_at": _now_iso(),
                "started_ts": time.time(),
            }
        self._emit(None, RUN_START, label=label, params=params or {}, run_id=run_id)

    def run_end(self, status: str = "completed", error: Optional[str] = None) -> None:
        with self._lock:
            started = self._run.get("started_ts")
            duration_ms = round((time.time() - started) * 1000, 1) if started else None
            self._run.update(
                {"status": status, "ended_at": _now_iso(), "duration_ms": duration_ms}
            )
        self._emit(
            None,
            RUN_END,
            status=status,
            duration_ms=duration_ms,
            error=_truncate(error, _MAX_PREVIEW) if error is not None else None,
        )

    # ----------------------------------------------------------- text streaming
    def buffer_text(self, agent: str, delta: str) -> None:
        with self._lock:
            self._text_buf[agent] = self._text_buf.get(agent, "") + delta

    def buffer_reasoning(self, agent: str, delta: str) -> None:
        with self._lock:
            self._reason_buf[agent] = self._reason_buf.get(agent, "") + delta

    def flush_text(self, agent: str) -> None:
        with self._lock:
            reasoning = self._reason_buf.pop(agent, "").strip()
            text = self._text_buf.pop(agent, "").strip()
        if reasoning:
            self._emit(agent, TEXT, text=_truncate(reasoning, _MAX_TEXT), reasoning=True)
        if text:
            self._emit(agent, TEXT, text=_truncate(text, _MAX_TEXT), reasoning=False)

    # -------------------------------------------------------------- tool edges
    def tool_start(
        self, agent: str, tool_name: str, tool_input: Any, tool_use_id: str
    ) -> None:
        # Flush any text the agent streamed just before deciding to call a tool.
        self.flush_text(agent)
        # Per-context, so concurrently-running tools do not overwrite each
        # other's attribution. _current stays a plain field: it only drives
        # the "now running" badge, where last-started is the right answer.
        _ACTIVE_TOOL.set((agent, tool_name, tool_use_id))
        with self._lock:
            self._open_tools[tool_use_id] = (time.time(), agent, tool_name)
            self._current = (agent, tool_name)
        self._emit(
            agent,
            TOOL_START,
            tool_name=tool_name,
            tool_input=_jsonable(tool_input),
            tool_use_id=tool_use_id,
        )

    def tool_end(
        self,
        agent: str,
        tool_name: str,
        tool_use_id: str,
        result: Any,
        exception: Optional[BaseException] = None,
    ) -> None:
        _ACTIVE_TOOL.set((None, None, None))
        with self._lock:
            started, _, _ = self._open_tools.pop(tool_use_id, (None, agent, tool_name))
            duration_ms = round((time.time() - started) * 1000, 1) if started else None
            self._current = (None, None)
        summary = _extract_tool_result(exception if exception is not None else result)
        self._emit(
            agent,
            TOOL_END,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            status=summary["status"] or ("error" if exception else "success"),
            row_count=summary["row_count"],
            result_preview=summary["preview"],
            duration_ms=duration_ms,
        )

    # --------------------------------------------------------------- agent edges
    def agent_start(self, agent: str) -> None:
        self._emit(agent, AGENT_START)

    def agent_end(self, agent: str) -> None:
        self.flush_text(agent)
        self._emit(agent, AGENT_END)

    # ------------------------------------------------------------------- queries
    def log_query(self, sql: str, params: Any, result: dict) -> None:
        """Called from the DB chokepoint with the *actual* SQL that ran.

        This is what surfaces "the code they're running": the literal query
        text, its parameters, the row count and execution time, attributed to
        the tool that actually ran it — read from the _ACTIVE_TOOL ContextVar,
        so parallel tool calls each keep their own attribution. A query with
        no tool in context (e.g. the pre-run window check) is genuinely
        agent-less and is shown under the run itself.
        """
        agent, tool, tool_use_id = _ACTIVE_TOOL.get()
        self._emit(
            agent,
            QUERY,
            tool_name=tool,
            tool_use_id=tool_use_id,
            sql=_truncate(_dedent_sql(sql), _MAX_PREVIEW),
            params=_jsonable(params),
            row_count=result.get("row_count") if isinstance(result, dict) else None,
            execution_time_ms=(
                result.get("execution_time_ms") if isinstance(result, dict) else None
            ),
            ok=bool(result.get("success", True)) if isinstance(result, dict) else True,
            error=(
                _truncate(result.get("error"), _MAX_PREVIEW)
                if isinstance(result, dict) and result.get("error") is not None
                else None
            ),
        )

    def error(self, agent: Optional[str], message: str) -> None:
        self._emit(agent, ERROR, message=_truncate(message, _MAX_PREVIEW))


def _dedent_sql(sql: str) -> str:
    """Trim the heavy leading indentation the triple-quoted SQL literals carry."""
    lines = [ln.rstrip() for ln in str(sql).strip("\n").splitlines()]
    stripped = [ln for ln in lines if ln.strip()]
    if not stripped:
        return str(sql).strip()
    indent = min(len(ln) - len(ln.lstrip()) for ln in stripped)
    return "\n".join(ln[indent:] if len(ln) >= indent else ln for ln in lines).strip()


def _jsonable(value: Any, _depth: int = 0) -> Any:
    """Best-effort, bounded conversion of tool inputs to displayable data."""
    if _depth >= _MAX_JSON_DEPTH:
        return "<nested value truncated>"
    if isinstance(value, str):
        return _truncate(value, _MAX_PREVIEW)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        converted = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                break
            converted[_truncate(str(key), 200)] = _jsonable(item, _depth + 1)
        if len(value) > _MAX_COLLECTION_ITEMS:
            converted["<truncated>"] = (
                f"{len(value) - _MAX_COLLECTION_ITEMS} more entries"
            )
        return converted
    if isinstance(value, (list, tuple)):
        converted = [
            _jsonable(item, _depth + 1)
            for item in value[:_MAX_COLLECTION_ITEMS]
        ]
        if len(value) > _MAX_COLLECTION_ITEMS:
            converted.append(
                f"<{len(value) - _MAX_COLLECTION_ITEMS} more entries>"
            )
        return converted
    return _truncate(value, _MAX_PREVIEW)


class _TracingCallback:
    """Per-agent Strands callback handler: captures streamed text + reasoning."""

    def __init__(self, tracer: AgentTracer, label: str) -> None:
        self._tracer = tracer
        self._label = label

    def __call__(self, **kwargs: Any) -> None:
        reasoning = kwargs.get("reasoningText")
        data = kwargs.get("data")
        complete = kwargs.get("complete")
        if reasoning:
            self._tracer.buffer_reasoning(self._label, reasoning)
        if data:
            self._tracer.buffer_text(self._label, data)
        if complete:
            self._tracer.flush_text(self._label)


class _TracingHooks(HookProvider):
    """Per-agent hook provider: agent + tool lifecycle boundaries and results."""

    def __init__(self, tracer: AgentTracer, label: str) -> None:
        self._tracer = tracer
        self._label = label

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_agent_start)
        registry.add_callback(AfterInvocationEvent, self._on_agent_end)
        registry.add_callback(BeforeToolCallEvent, self._on_tool_start)
        registry.add_callback(AfterToolCallEvent, self._on_tool_end)

    def _on_agent_start(self, event: BeforeInvocationEvent) -> None:
        self._tracer.agent_start(self._label)

    def _on_agent_end(self, event: AfterInvocationEvent) -> None:
        self._tracer.agent_end(self._label)

    def _on_tool_start(self, event: BeforeToolCallEvent) -> None:
        tu = event.tool_use
        self._tracer.tool_start(
            self._label, tu.get("name", "?"), tu.get("input"), tu.get("toolUseId", "")
        )

    def _on_tool_end(self, event: AfterToolCallEvent) -> None:
        tu = event.tool_use
        self._tracer.tool_end(
            self._label,
            tu.get("name", "?"),
            tu.get("toolUseId", ""),
            event.result,
            exception=getattr(event, "exception", None),
        )


def attach_tracer(agent: Any, label: str, tracer: AgentTracer) -> None:
    """Wire a tracer into a Strands ``Agent`` without raw terminal output.

    Strands' default callback streams model-controlled text directly to stdout.
    The trace callback retains the live dashboard feed while keeping that
    untrusted text out of operator terminals.
    """
    agent.callback_handler = _TracingCallback(tracer, label)
    agent.hooks.add_hook(_TracingHooks(tracer, label))
