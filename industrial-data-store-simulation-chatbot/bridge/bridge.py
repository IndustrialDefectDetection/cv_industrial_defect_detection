"""
STEP 5 Bridge — FastAPI app on port 8081 (CONTRACTS.md).

Endpoints:
    POST /detection  — receive detection payload from the simulator
    GET  /health     — health check

Behavior:
    - Saves every incoming detection to VisionDetections immediately.
    - Looks up the active work order for the machine (OrderID stored if found).
    - Applies the 0.80 confidence gate — only gated detections feed into
      the per-machine 30-second batch window.
    - When a window closes, analyze_batch() is called in a background thread.
"""

import logging

import psycopg2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from bridge.batch_manager import BatchManager
from bridge.db_config import (
    CONF_GATE,
    PG_HOST,
    PG_PORT,
    PG_USER,
    PG_PASSWORD,
    PG_DBNAME,
)
from bridge.mes_lookups import get_active_work_order

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App & batch manager (singleton)
# ---------------------------------------------------------------------------
app = FastAPI(title="Defect Detection Bridge", version="1.0.0")
batch_manager = BatchManager()


# ---------------------------------------------------------------------------
# Pydantic models matching CONTRACTS.md §2 payload shape
# ---------------------------------------------------------------------------
class BBox(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class DetectionItem(BaseModel):
    class_: str
    class_id: int
    confidence: float
    bbox: BBox


class DetectionPayload(BaseModel):
    timestamp: str
    machine_id: int
    image_name: str
    inference_time_ms: float
    detections: list[DetectionItem]


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------
def _get_conn():
    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=PG_DBNAME,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """Health check — also verifies DB connectivity."""
    try:
        conn = _get_conn()
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok", "database": db_ok}


@app.post("/detection")
async def handle_detection(payload: DetectionPayload):
    """
    Receive one detection payload (per CONTRACTS.md §2).

    1. Look up the active work order for the machine.
    2. Save every individual detection to VisionDetections.
    3. If confidence >= 0.80, feed into BatchManager.
    """
    # Resolve active work order (nullable)
    # RealDictCursor returns lowercased column names
    order = get_active_work_order(payload.machine_id)
    order_id = order["orderid"] if order else None

    # Per-image timestamp comes from the payload (CONTRACTS.md §2)
    payload_ts = payload.timestamp

    saved_ids: list[int] = []

    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            for det in payload.detections:
                bbox_json = det.bbox.model_dump_json()

                cur.execute(
                    """
                    INSERT INTO VisionDetections
                        (Timestamp, MachineID, OrderID, DefectType,
                         Confidence, BBox, ImageName, InferenceTimeMs)
                    VALUES
                        (%s, %s, %s, %s,
                         %s, %s, %s, %s)
                    RETURNING DetectionID
                    """,
                    (
                        payload_ts,
                        payload.machine_id,
                        order_id,
                        det.class_,
                        det.confidence,
                        bbox_json,
                        payload.image_name,
                        payload.inference_time_ms,
                    ),
                )
                detection_id = cur.fetchone()[0]
                saved_ids.append(detection_id)

                # Confidence gate — feed into batch manager
                if det.confidence >= CONF_GATE:
                    batch_manager.feed(
                        {
                            "machine_id": payload.machine_id,
                            "order_id": order_id,
                            "detection_id": detection_id,
                            "timestamp": payload_ts,
                            "class": det.class_,
                            "confidence": det.confidence,
                            "image_name": payload.image_name,
                        }
                    )

        conn.commit()
        conn.close()

    except Exception as exc:
        logger.error("Failed to save detection batch: %s", exc, exc_info=True)
        # Roll back any partial INSERTs (pgloader auto-closes on error)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info(
        "Saved %s detection(s) from machine %s (order_id=%s); "
        "%s passed confidence gate",
        len(saved_ids),
        payload.machine_id,
        order_id,
        sum(1 for d in payload.detections if d.confidence >= CONF_GATE),
    )

    return {
        "saved_count": len(saved_ids),
        "detection_ids": saved_ids,
        "order_id": order_id,
    }