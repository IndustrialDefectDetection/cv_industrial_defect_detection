"""Under-the-hood trace viewer for the MES agent backend — the project's
observability dashboard for watching what the agents are doing, live.

    streamlit run trace_viewer.py --server.port 8502

(Also launched automatically by the repo-root Launch.py alongside the API and
the Next.js frontend.)

Talks to the FastAPI backend (api.py, port 8000) purely over HTTP — the same
three endpoints documented in TRACE_API.md that any other frontend would use:

    GET  /health     is the backend usable, and if not, why
    GET  /trace      the live event stream from agent_tracer.AgentTracer
    GET  /defect-types  populates the event-type dropdown
    POST /analysis   trigger the structured defect-analysis workflow
    POST /chat/      free-text supervisor run (used by the Next.js chat page;
                     runs from any client show up here regardless)

Because it never imports strands_agent, this viewer works no matter which
process or UI started the run, and it doubles as a reference client for the
trace API contract.
"""

import json
import threading
import time

import requests
import streamlit as st

DEFAULT_BASE = "http://127.0.0.1:8000"
POLL_RUNNING = 0.8  # trace poll cadence while a run is active
POLL_IDLE = 2.5  # keep polling when idle so runs from other clients appear

# Streamlit markdown accent color per agent, so the timeline is scannable.
AGENT_COLORS = {
    "Run": "gray",
    "Supervisor": "violet",
    "Monitor": "blue",
    "Analyzer": "green",
    "Planner": "orange",
    "Verifier": "gray",
    "Executor": "red",
}

st.set_page_config(
    page_title="Under the Hood — MES Agent Trace",
    page_icon="🔍",
    layout="wide",
)


# --------------------------------------------------------------------- helpers
def fetch_json(url: str, timeout: float = 3.0):
    """GET a JSON endpoint. Returns (data, None) or (None, error_message)."""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json(), None
    except requests.exceptions.ConnectionError:
        return None, f"Backend unreachable at {url} — is `python startup.py --api` running?"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def post_analysis(base_url: str, params: dict, holder: dict) -> None:
    """POST /analysis from a background thread; writes only into `holder`.

    Sends the control values as structured parameters — the backend's
    run_defect_analysis builds the supervisor prompt itself, so the scope
    flags arrive as data rather than as prose the model has to interpret.

    Must not touch any st.* API — Streamlit objects are not thread-safe. The
    main script reruns on its poll interval and reads `holder` instead.
    """
    try:
        resp = requests.post(
            f"{base_url}/analysis",
            json=params,
            timeout=1800,  # full five-agent runs take minutes
        )
        if resp.ok:
            body = resp.json()
            holder["status"] = "failed" if body.get("status") == "failed" else "completed"
            holder["analysis"] = body.get("supervisor_orchestration", "")
            holder["duration_s"] = body.get("total_duration")
            if body.get("error"):
                holder["detail"] = body["error"]
        else:
            holder["status"] = "failed"
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            holder["detail"] = f"HTTP {resp.status_code}: {detail}"
    except Exception as e:
        holder["status"] = "failed"
        holder["detail"] = f"{type(e).__name__}: {e}"


def group_events(events: list[dict]) -> tuple[list[dict], dict]:
    """Group the flat event stream into consecutive per-agent sections.

    Events with agent=None (run_start/run_end/top-level errors) get a
    synthetic "Run" group. tool_end events are matched back to their
    tool_start by tool_use_id so the tool's card can show duration/result;
    a matched tool_end is not rendered as a separate row, but one whose
    tool_start was trimmed from the ring buffer still gets its own line.
    """
    ends_by_id = {
        e["tool_use_id"]: e
        for e in events
        if e["kind"] == "tool_end" and e.get("tool_use_id")
    }
    start_ids = {
        e["tool_use_id"]
        for e in events
        if e["kind"] == "tool_start" and e.get("tool_use_id")
    }
    groups: list[dict] = []
    for event in events:
        agent = event["agent"] or "Run"
        if not groups or groups[-1]["agent"] != agent:
            groups.append({"agent": agent, "events": [], "done": False})
        if event["kind"] == "agent_end":
            groups[-1]["done"] = True
        if event["kind"] == "tool_end" and event.get("tool_use_id") in start_ids:
            continue  # rendered as part of its tool_start card
        groups[-1]["events"].append(event)
    return groups, ends_by_id


def as_blockquote(text: str) -> str:
    return "> " + text.replace("\n", "\n> ")


# ------------------------------------------------------------------- renderers
def render_tool_card(event: dict, ends_by_id: dict) -> None:
    end = ends_by_id.get(event.get("tool_use_id"))
    bits = [f"🔧 **`{event['tool_name']}`**"]
    if end:
        if end.get("duration_ms") is not None:
            bits.append(f"{end['duration_ms']:.0f} ms")
        if end.get("row_count") is not None:
            bits.append(f"{end['row_count']} rows")
        status = end.get("status") or "success"
        bits.append(":red[error]" if status == "error" else f":green[{status}]")
    else:
        bits.append(":blue[running…]")
    st.markdown(" · ".join(bits))
    if event.get("tool_input"):
        st.caption("arguments")
        st.json(event["tool_input"], expanded=False)
    if end and end.get("result_preview"):
        st.caption("result preview")
        st.code(end["result_preview"], language=None)


def render_query(event: dict) -> None:
    st.caption(f"SQL via `{event.get('tool_name') or '?'}`")
    st.code(event["sql"], language="sql")
    if event.get("ok"):
        meta = []
        if event.get("row_count") is not None:
            meta.append(f"{event['row_count']} rows")
        if event.get("execution_time_ms") is not None:
            meta.append(f"{event['execution_time_ms']} ms")
        if event.get("params"):
            meta.append(f"params: {json.dumps(event['params'])}")
        if meta:
            st.caption(" · ".join(meta))
    else:
        st.error(f"Query failed: {event.get('error')}")


def render_event(event: dict, ends_by_id: dict) -> None:
    kind = event["kind"]
    if kind == "tool_start":
        render_tool_card(event, ends_by_id)
    elif kind == "tool_end":  # only unmatched ones reach here
        st.markdown(f"🔧 `{event['tool_name']}` finished · {event.get('status')}")
    elif kind == "query":
        render_query(event)
    elif kind == "text":
        if event.get("reasoning"):
            st.caption("💭 reasoning")
        st.markdown(as_blockquote(event["text"]))
    elif kind == "error":
        st.error(event["message"])
    elif kind == "run_start":
        st.markdown(f"▶️ **{event['label']}**  \nrun `{event.get('run_id', '?')}`")
        if event.get("params"):
            st.json(event["params"], expanded=False)
    elif kind == "run_end":
        icon = "✅" if event["status"] == "completed" else "❌"
        line = f"{icon} run **{event['status']}**"
        if event.get("duration_ms") is not None:
            line += f" in {event['duration_ms'] / 1000:.1f}s"
        st.markdown(line)
        if event.get("error"):
            st.error(event["error"])
    # agent_start / agent_end are folded into the group header, nothing to draw.


# -------------------------------------------------------------------- sidebar
st.sidebar.title("🔍 Under the Hood")
base_url = st.sidebar.text_input("Backend URL", DEFAULT_BASE).rstrip("/")
live = st.sidebar.toggle("Live updates", value=True)

health, health_err = fetch_json(f"{base_url}/health")
st.sidebar.subheader("Backend health")
if health_err:
    st.sidebar.error(health_err)
else:
    st.sidebar.markdown(":green[●] connected")
    st.sidebar.caption(f"model: `{health['model_id']}`")
    st.sidebar.caption(f"db: `{health['db_path']}`")
    if not health["db_exists"]:
        st.sidebar.error("mes.db not found at that path")
    if not health["anthropic_api_key_set"]:
        st.sidebar.error("ANTHROPIC_API_KEY is not set (check .env)")
    if health["agent_manager_ready"]:
        st.sidebar.markdown(":green[●] agent manager ready")
    else:
        st.sidebar.error(f"Agent manager failed to start: {health['agent_manager_error']}")

st.sidebar.divider()
st.sidebar.caption(
    "This viewer only calls the HTTP API (`/health`, `/trace`, `/chat/`) — "
    "see TRACE_API.md. Runs triggered from any client appear here."
)

if health_err:
    st.title("Under the Hood")
    st.error(health_err)
    if st.button("Retry"):
        st.rerun()
    st.stop()

# ---------------------------------------------------------------- trigger box
st.title("Under the Hood")
st.caption("Live trace of the Supervisor → Monitor / Analyzer / Planner / Verifier / Executor workflow")

chat = st.session_state.setdefault("chat", {"status": "idle"})
qa_history = st.session_state.setdefault("qa_history", [])
chat_running = chat["status"] == "running"

# The supervisor Agent keeps conversation state across /chat/ calls, so past
# turns matter for reading a run. Record each finished turn exactly once.
if chat["status"] in ("completed", "failed") and not chat.get("recorded"):
    chat["recorded"] = True
    qa_history.append(
        {
            "question": chat.get("question", "?"),
            "status": chat["status"],
            "answer": chat.get("analysis") or chat.get("detail") or "",
        }
    )

@st.cache_data(ttl=300)
def load_defect_types(base: str) -> list[str]:
    data, err = fetch_json(f"{base}/defect-types", timeout=10)
    if err or not data:
        return []
    return data.get("defect_types", [])


defect_options = load_defect_types(base_url)

with st.form("trigger", clear_on_submit=False):
    top = st.columns([2, 1])
    selected_defect = top[0].selectbox(
        "Event / defect type",
        options=[None] + defect_options,
        format_func=lambda x: "-- Select an event type --" if x is None else x,
        disabled=chat_running,
    )
    period = top[1].selectbox(
        "Look back period",
        ["Last 3 days", "Last 7 days", "Last 14 days", "Last 30 days", "Last 120 days"],
        index=1,
        disabled=chat_running,
    )
    scope_cols = st.columns(4)
    include_oee = scope_cols[0].checkbox("OEE performance", value=False, disabled=chat_running)
    include_downtime = scope_cols[1].checkbox("Downtime & stoppages", value=False, disabled=chat_running)
    include_changeover = scope_cols[2].checkbox("Batch changeover", value=False, disabled=chat_running)
    include_maintenance = scope_cols[3].checkbox("Maintenance correlation", value=True, disabled=chat_running)
    submitted = st.form_submit_button(
        "🚀 Run Analysis", disabled=chat_running, use_container_width=True
    )

if not defect_options:
    st.warning(
        "No defect types available from the backend — is it running, and does mes.db have data?"
    )

if submitted and not chat_running:
    if selected_defect is None:
        st.warning("Select an event type first.")
    else:
        days_back = int(period.split()[1])
        params = {
            "defect_type": selected_defect,
            "days_back": days_back,
            "include_oee": include_oee,
            "include_downtime": include_downtime,
            "include_changeover": include_changeover,
            "include_maintenance": include_maintenance,
        }
        chat = {"status": "running", "question": f"{selected_defect} · last {days_back}d"}
        st.session_state["chat"] = chat
        threading.Thread(
            target=post_analysis, args=(base_url, params, chat), daemon=True
        ).start()

# Earlier turns from this session, collapsed, oldest first.
for turn in qa_history[:-1]:
    icon = "💬" if turn["status"] == "completed" else "❌"
    with st.expander(f"{icon} {turn['question']}", expanded=False):
        st.markdown(turn["answer"] or "*empty response*")

if chat["status"] == "failed":
    st.error(f"Chat request failed — {chat.get('detail')}")
elif chat["status"] == "completed":
    label = "💬 Final report from the supervisor"
    if chat.get("duration_s"):
        label += f" · {chat['duration_s']:.0f}s"
    with st.expander(label, expanded=False):
        st.markdown(chat.get("analysis") or "*empty response*")

# --------------------------------------------------------------- trace panel
# The whole trace view lives in a fragment so live polling reruns only this
# section — the trigger form above is never disturbed. It polls even when
# idle (slower cadence), so runs triggered from other clients (the Next.js
# chat, curl) appear without touching the page. When the active/idle state
# flips — or the background chat thread finishes — the fragment requests a
# full rerun to re-arm run_every and refresh the chat section above.
st.session_state["last_chat_status"] = chat["status"]
trace_active = st.session_state.setdefault("trace_active", False)
run_every = (POLL_RUNNING if trace_active else POLL_IDLE) if live else None


def agent_time_line(events: list[dict]) -> str:
    """'Supervisor 87s · Monitor 12s · …' from agent_start/agent_end pairs.

    An agent invoked several times gets its spans summed; one still running
    counts up to now and is marked with an ellipsis. The Supervisor's span is
    roughly the whole run, since the sub-agents work inside its tool calls.
    """
    open_starts: dict[str, list[float]] = {}
    totals: dict[str, float] = {}
    order: list[str] = []
    for event in events:
        agent = event["agent"]
        if event["kind"] == "agent_start":
            open_starts.setdefault(agent, []).append(event["ts"])
            if agent not in order:
                order.append(agent)
        elif event["kind"] == "agent_end" and open_starts.get(agent):
            totals[agent] = totals.get(agent, 0.0) + event["ts"] - open_starts[agent].pop()
    bits = []
    for agent in order:
        running = bool(open_starts.get(agent))
        if running:
            totals[agent] = totals.get(agent, 0.0) + time.time() - open_starts[agent][-1]
        if agent in totals:
            bits.append(f"{agent} {totals[agent]:.0f}s" + ("…" if running else ""))
    return " · ".join(bits)


@st.fragment(run_every=run_every)
def trace_section() -> None:
    snapshot, trace_err = fetch_json(f"{base_url}/trace?since=0", timeout=5)
    if trace_err:
        st.error(trace_err)
        return

    run = snapshot.get("run", {})
    current = snapshot.get("current", {})
    events = snapshot.get("events", [])
    run_status = run.get("status", "idle")

    cols = st.columns([1, 3, 1])
    with cols[0]:
        badge = {
            "idle": ":gray[● idle]",
            "running": ":blue[● running]",
            "completed": ":green[● completed]",
            "failed": ":red[● failed]",
        }.get(run_status, run_status)
        st.markdown(f"### {badge}")
    with cols[1]:
        if run.get("label"):
            st.markdown(f"**{run['label']}**")
            st.caption(f"run `{run.get('run_id', '?')}` · started {run.get('started_at', '?')}")
        if run_status == "running" and current.get("agent"):
            now_line = current["agent"]
            if current.get("tool"):
                now_line += f" → `{current['tool']}`"
            st.markdown(f"⏳ now: **{now_line}**")
    with cols[2]:
        if run_status == "running" and run.get("started_ts"):
            st.metric("elapsed", f"{time.time() - run['started_ts']:.0f}s")
        elif run.get("duration_ms") is not None:
            st.metric("duration", f"{run['duration_ms'] / 1000:.1f}s")

    # ------------------------------------------------------------ stats strip
    if events:
        queries = [e for e in events if e["kind"] == "query"]
        metrics = st.columns(4)
        metrics[0].metric("tool calls", sum(1 for e in events if e["kind"] == "tool_start"))
        metrics[1].metric("SQL queries", len(queries))
        metrics[2].metric("rows fetched", sum(e.get("row_count") or 0 for e in queries))
        metrics[3].metric("errors", sum(1 for e in events if e["kind"] == "error"))
        times = agent_time_line(events)
        if times:
            st.caption(f"time per agent: {times}")

    # -------------------------------------------------------------- timeline
    if not events:
        st.info("No trace yet. Pick an event type above and click Run Analysis to watch the agents work.")
    else:
        groups, ends_by_id = group_events(events)
        for group in groups:
            color = AGENT_COLORS.get(group["agent"], "gray")
            tick = " · ✓ done" if group["done"] else ""
            label = f":{color}[**{group['agent']}**] · {len(group['events'])} events{tick}"
            with st.expander(label, expanded=True):
                for event in group["events"]:
                    render_event(event, ends_by_id)

        st.divider()
        raw = json.dumps({"run": run, "current": current, "events": events}, indent=2, default=str)
        st.download_button(
            "⬇️ Download trace JSON (attach to bug reports)",
            data=raw,
            file_name=f"trace_{run.get('run_id', 'empty')}.json",
            mime="application/json",
            key="download-trace",
        )
        with st.expander(f"Raw events ({len(events)})", expanded=False):
            st.json(events, expanded=False)

    # -------------------------------------------------- poll-cadence control
    chat_status = st.session_state.get("chat", {}).get("status", "idle")
    active = run_status == "running" or chat_status == "running"
    if active != st.session_state.get("trace_active") or chat_status != st.session_state.get(
        "last_chat_status"
    ):
        st.session_state["trace_active"] = active
        st.rerun()  # scope="app": re-arm run_every + refresh the chat section


trace_section()
