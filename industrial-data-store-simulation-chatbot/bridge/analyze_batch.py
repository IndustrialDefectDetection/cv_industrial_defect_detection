"""
STEP 6 The seam — `analyze_batch` (CONTRACTS.md).

Called by the bridge once per closed batch window.
Inserts a 'pending' row into AgentAlerts and returns the AlertID
without waiting for the LLM (the ~60s agent run happens in a background thread).
Never raises — failures mark the alert 'failed'.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import psycopg2
from bridge.db_config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME

logger = logging.getLogger(__name__)


def _get_conn():
    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=PG_DBNAME,
    )


def analyze_batch(batch: dict[str, Any]) -> int:
    """
    Insert a 'pending' AgentAlert row and return its AlertID.

    The LLM analysis runs in a background thread (caller's responsibility).
    This function must return in <1s — it only performs a single INSERT.

    ``batch`` shape (built by the bridge's batch_manager):
    {
        "machine_id": 12,
        "order_id": 4471 | None,
        "window_start": "2026-07-19T14:02:00Z",
        "window_end":   "2026-07-19T14:02:30Z",
        "detections": [
            {
                "detection_id": 913,
                "timestamp": "2026-07-19T14:02:31Z",
                "class": "scratches",
                "confidence": 0.8712,
                "image_name": "scratches_101.jpg"
            },
            ...
        ]
    }
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # Determine dominant defect class (most frequent)
    classes = [d["class"] for d in batch["detections"]]
    dominant_class = max(set(classes), key=classes.count) if classes else "unknown"

    sql = """
        INSERT INTO AgentAlerts
            (CreatedAt, MachineID, OrderID, DefectType, DetectionCount,
             WindowStart, WindowEnd, Status)
        VALUES
            (%s, %s, %s, %s, %s,
             %s, %s, 'pending')
        RETURNING AlertID
    """
    params = (
        now_iso,
        batch["machine_id"],
        batch.get("order_id"),
        dominant_class,
        len(batch["detections"]),
        batch["window_start"],
        batch["window_end"],
    )

    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            alert_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        logger.info("AgentAlert %s inserted (pending) for machine %s", alert_id, batch["machine_id"])
        return alert_id
    except Exception as exc:
        logger.error("analyze_batch INSERT failed: %s", exc, exc_info=True)
        return -1