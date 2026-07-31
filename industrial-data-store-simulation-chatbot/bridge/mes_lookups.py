"""
§4 Lookup helpers — CONTRACTS.md.

Queried live (never cached) against the PostgreSQL database so they
always reflect the current data generator state.
"""

import psycopg2
import psycopg2.extras
import logging
from bridge.db_config import connection_kwargs
from app_factory.shared.display_security import safe_log_text

logger = logging.getLogger(__name__)


def _get_conn():
    """Return a fresh PostgreSQL connection to the target database."""
    return psycopg2.connect(**connection_kwargs())


def get_frame_machines() -> list[dict]:
    """
    Return all Frame Welding machines.

    SELECT MachineID, Name, Status, WorkCenterID
      FROM Machines
     WHERE Type = 'Frame Welding'

    Returns [] if none.  Never cached.
    """
    sql = """
        SELECT MachineID, Name, Status, WorkCenterID
          FROM Machines
         WHERE Type = 'Frame Welding'
    """
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("get_frame_machines failed: %s", safe_log_text(exc))
        return []
    finally:
        if conn is not None:
            conn.close()


def get_active_work_order(machine_id: int) -> dict | None:
    """
    Return the currently in-progress work order for a machine, or None.

    SELECT OrderID, ProductID, LotNumber, Status, ActualStartTime
      FROM WorkOrders
     WHERE MachineID = %s AND Status = 'in_progress'
     ORDER BY ActualStartTime DESC LIMIT 1
    """
    sql = """
        SELECT OrderID, ProductID, LotNumber, Status, ActualStartTime
          FROM WorkOrders
         WHERE MachineID = %s AND Status = 'in_progress'
         ORDER BY ActualStartTime DESC
         LIMIT 1
    """
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (machine_id,))
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.error(
            "get_active_work_order(machine_id=%s) failed: %s",
            machine_id,
            safe_log_text(exc),
        )
        return None
    finally:
        if conn is not None:
            conn.close()
