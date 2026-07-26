"""
§6 seam — CONTRACTS.md. The agent side of the bridge.

    bridge.py  →  batch_manager.py  →  analyze_batch()  →  AgentAlerts

`batch_manager` calls this once per closed 30-second window, with every
gated (confidence >= 0.80) detection collected in it. Ownership per §3: the
bridge writes only VisionDetections; this module is the only writer of
AgentAlerts; the dashboard only reads.

The three rules this file exists to honour:

  * Return in under a second. The agent run takes ~60s, and the bridge is
    servicing a live camera feed - blocking here backs detections up.
    So: INSERT a 'pending' row, return its AlertID, do the slow work on a
    background thread.
  * Never raise. batch_manager logs whatever comes out of here, but an
    exception would leave the row stuck in 'pending' forever. Failures are
    recorded as status='failed' so they are visible in the dashboard.
  * Carry status, not just a report, so the dashboard can show
    "analyzing..." while the agent is still thinking.

STAGE 1 (plan task 9): the lifecycle is wired end to end with NO LLM call -
the background worker writes a clearly-labelled placeholder report. This
proves camera → API → bridge → gate → batching → seam → database at zero
API cost. Task 10 replaces the body of `_run_agent` with the real agent run;
nothing else in this file needs to change.
"""

import logging
import threading
from collections import Counter
from datetime import datetime, timezone

import psycopg2

from bridge.db_config import (
    PG_DBNAME,
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    PG_USER,
)

logger = logging.getLogger(__name__)

# Flip to False in task 10, once _run_agent actually calls the agent.
STUB_MODE = True

_STUB_REPORT = (
    "**[stub - no agent run]**\n\n"
    "The seam is wired and this alert completed its full lifecycle "
    "(pending → analyzing → done) without calling an LLM. "
    "Task 10 replaces this with the real root-cause report."
)


def _get_conn():
    """Fresh connection, same settings the rest of the bridge uses."""
    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=PG_DBNAME,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_tables() -> None:
    """Create AgentAlerts if absent — this module owns the table (§3).

    Idempotent and safe to call on every batch: the data generator can wipe
    and rebuild the database underneath a long-running bridge.
    """
    sql = """
    CREATE TABLE IF NOT EXISTS AgentAlerts (
        AlertID         SERIAL PRIMARY KEY,
        CreatedAt       TIMESTAMPTZ NOT NULL,
        MachineID       INTEGER NOT NULL,
        OrderID         INTEGER,
        DefectType      TEXT NOT NULL,
        DetectionCount  INTEGER NOT NULL,
        WindowStart     TIMESTAMPTZ NOT NULL,
        WindowEnd       TIMESTAMPTZ NOT NULL,
        Status          TEXT NOT NULL DEFAULT 'pending',
        Report          TEXT,
        CompletedAt     TIMESTAMPTZ
    );
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _dominant_defect_type(detections: list[dict]) -> str:
    """The batch's headline class: most frequent, ties broken by confidence.

    A burst is rarely one class only, and the alert carries a single
    DefectType (§3), so pick the one that best characterises the window.
    """
    if not detections:
        return "unknown"
    counts = Counter(d.get("class", "unknown") for d in detections)
    top = max(counts.values())
    tied = [cls for cls, n in counts.items() if n == top]
    if len(tied) == 1:
        return tied[0]
    best_conf = {
        cls: max((d.get("confidence") or 0) for d in detections
                 if d.get("class") == cls)
        for cls in tied
    }
    return max(best_conf, key=best_conf.get)


def _set_status(alert_id: int, status: str, report: str | None = None) -> None:
    """Move an alert along its lifecycle. Best-effort: never raises."""
    try:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                if status == "done":
                    cur.execute(
                        "UPDATE AgentAlerts SET Status=%s, Report=%s, CompletedAt=%s "
                        "WHERE AlertID=%s",
                        (status, report, _utc_now(), alert_id),
                    )
                elif status == "failed":
                    cur.execute(
                        "UPDATE AgentAlerts SET Status=%s, Report=%s, CompletedAt=%s "
                        "WHERE AlertID=%s",
                        (status, report, _utc_now(), alert_id),
                    )
                else:
                    cur.execute(
                        "UPDATE AgentAlerts SET Status=%s WHERE AlertID=%s",
                        (status, alert_id),
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        # Nothing useful left to do - the run is already over.
        logger.error("Could not set alert %s to %s: %s", alert_id, status, exc)


def _run_agent(alert_id: int, batch: dict) -> None:
    """Background worker: the slow half of the seam.

    TASK 10 REPLACES THE BODY BELOW. Keep the contract: set 'analyzing'
    first, write the markdown into Report, finish 'done' or 'failed', and
    swallow every exception.
    """
    try:
        _set_status(alert_id, "analyzing")

        if STUB_MODE:
            logger.info(
                "[stub] alert %s: machine=%s order=%s %s detections %s → %s",
                alert_id,
                batch.get("machine_id"),
                batch.get("order_id"),
                len(batch.get("detections", [])),
                batch.get("window_start"),
                batch.get("window_end"),
            )
            for det in batch.get("detections", []):
                logger.info(
                    "[stub]   detection %s %s conf=%.3f %s",
                    det.get("detection_id"), det.get("class"),
                    det.get("confidence") or 0.0, det.get("image_name"),
                )
            _set_status(alert_id, "done", _STUB_REPORT)
            return

        # --- task 10 goes here -------------------------------------------
        raise NotImplementedError("Real agent run not implemented yet")

    except Exception as exc:
        logger.error("Agent run failed for alert %s: %s", alert_id, exc, exc_info=True)
        _set_status(alert_id, "failed", f"Agent run failed: {exc}")


def analyze_batch(batch: dict) -> int:
    """Called by the bridge once per closed batch window.

    Immediately inserts a 'pending' row into AgentAlerts and RETURNS the
    AlertID without waiting for the LLM (must return in <1s; the ~60s agent
    run happens in a background thread). Never raises — failures mark the
    alert 'failed'.

    Returns the new AlertID, or -1 if the alert row could not be created
    (in which case there is no row to mark 'failed').
    """
    try:
        detections = batch.get("detections") or []
        defect_type = _dominant_defect_type(detections)

        ensure_tables()

        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO AgentAlerts
                        (CreatedAt, MachineID, OrderID, DefectType,
                         DetectionCount, WindowStart, WindowEnd, Status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                    RETURNING AlertID
                    """,
                    (
                        _utc_now(),
                        batch.get("machine_id"),
                        batch.get("order_id"),
                        defect_type,
                        len(detections),
                        batch.get("window_start"),
                        batch.get("window_end"),
                    ),
                )
                alert_id = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        logger.info(
            "Alert %s created: machine=%s %s×%s, window %s→%s",
            alert_id, batch.get("machine_id"), len(detections), defect_type,
            batch.get("window_start"), batch.get("window_end"),
        )

        # Hand the slow work off and return straight away.
        threading.Thread(
            target=_run_agent, args=(alert_id, batch), daemon=True
        ).start()

        return alert_id

    except Exception as exc:
        # No alert row exists to mark 'failed', so report it and let the
        # bridge carry on serving the camera feed.
        logger.error("analyze_batch could not create an alert: %s", exc, exc_info=True)
        return -1
