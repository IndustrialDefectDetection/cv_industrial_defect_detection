"""Under-the-hood trace viewer for the MES agent backend.

    streamlit run trace_viewer.py --server.port 8502

Talks to the FastAPI backend (api.py, port 8000) purely over HTTP — the same
three endpoints documented in TRACE_API.md that any other frontend would use:

    GET  /health   is the backend usable, and if not, why
    GET  /trace    the live event stream from agent_tracer.AgentTracer
    POST /chat/    trigger a traced supervisor run (also triggerable from the
                   Next.js chat page or curl — runs show up here regardless)

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
POLL_SECONDS = 0.8

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


def post_chat(base_url: str, user_input: str, holder: dict) -> None:
    """POST /chat/ from a background thread; writes only into `holder`.

    Must not touch any st.* API — Streamlit objects are not thread-safe. The
    main script reruns every POLL_SECONDS and reads `holder` instead.
    """
    try:
        resp = requests.post(
            f"{base_url}/chat/",
            json={"user_input": user_input},
            timeout=900,  # supervisor runs take minutes
        )
        if resp.ok:
            holder["status"] = "completed"
            holder["analysis"] = resp.json().get("analysis", "")
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
chat_running = chat["status"] == "running"

with st.form("trigger", clear_on_submit=False):
    question = st.text_input(
        "Ask the agent system (POSTs /chat/ and traces the run)",
        placeholder="e.g. Why are scratch defects up this week?",
    )
    submitted = st.form_submit_button("Send", disabled=chat_running)

if submitted and question.strip() and not chat_running:
    chat = {"status": "running", "question": question.strip()}
    st.session_state["chat"] = chat
    threading.Thread(
        target=post_chat, args=(base_url, question.strip(), chat), daemon=True
    ).start()

if chat["status"] == "failed":
    st.error(f"Chat request failed — {chat.get('detail')}")
elif chat["status"] == "completed":
    with st.expander("💬 Final answer from the supervisor", expanded=False):
        st.markdown(chat.get("analysis") or "*empty response*")

# ---------------------------------------------------------------- run header
snapshot, trace_err = fetch_json(f"{base_url}/trace?since=0", timeout=5)
if trace_err:
    st.error(trace_err)
    st.stop()

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

# ------------------------------------------------------------------ timeline
if not events:
    st.info("No trace yet. Send a message above (or from the chat frontend) to watch the agents work.")
else:
    groups, ends_by_id = group_events(events)
    for i, group in enumerate(groups):
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

# ------------------------------------------------------------------ live loop
# Plain polling: while a run is active, sleep briefly and rerun the script.
if live and (run_status == "running" or chat["status"] == "running"):
    time.sleep(POLL_SECONDS)
    st.rerun()
