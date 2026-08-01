"""
Per-machine batch window manager for CONTRACTS.md STEP 5 batching logic.

First gated detection (confidence >= 0.80) for a machine opens a 30-second
window. When the window expires all gated detections collected in it form
one batch and trigger analyze_batch().
"""

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from bridge.db_config import BATCH_WINDOW_SECONDS
from bridge.analyze_batch import analyze_batch
from app_factory.shared.display_security import safe_log_text

logger = logging.getLogger(__name__)


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


MAX_ACTIVE_WINDOWS = _bounded_int_env("MES_MAX_BATCH_WINDOWS", 32, 1, 32)
MAX_DETECTIONS_PER_WINDOW = _bounded_int_env(
    "MES_MAX_DETECTIONS_PER_WINDOW", 500, 1, 500
)


class BatchManager:
    """Manages per-machine batching windows."""

    def __init__(self):
        # machine_id -> {window_start, detections, timer}
        self._windows: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def feed(self, detection: dict[str, Any]) -> bool:
        """
        Feed a gated detection into the batching system.

        ``detection`` must have keys matching the §6 ``batch.detections[]`` shape:
            detection_id, timestamp, class, confidence, image_name

        The bridge calls this AFTER saving to VisionDetections and AFTER
        the 0.80 confidence gate.
        """
        machine_id = detection["machine_id"]
        with self._lock:
            if machine_id not in self._windows:
                if len(self._windows) >= MAX_ACTIVE_WINDOWS:
                    logger.warning(
                        "Ignoring gated detection: active-window limit reached"
                    )
                    return False
                # Open a new window
                now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                self._windows[machine_id] = {
                    "window_start": now_iso,
                    "detections": [],
                    "timer": None,
                }
                logger.info(
                    "Opened batch window for machine %s at %s",
                    machine_id, now_iso,
                )

            win = self._windows[machine_id]
            if len(win["detections"]) >= MAX_DETECTIONS_PER_WINDOW:
                logger.warning(
                    "Ignoring gated detection for machine %s: window limit reached",
                    machine_id,
                )
                return False
            win["detections"].append(detection)

            # Start the expiry timer on the first detection only
            if win["timer"] is None:
                timer = threading.Timer(
                    BATCH_WINDOW_SECONDS,
                    self._close_window,
                    args=[machine_id],
                )
                timer.daemon = True
                win["timer"] = timer
                timer.start()
            return True

    def _close_window(self, machine_id: int) -> None:
        """Timer callback — close the window and call analyze_batch."""
        with self._lock:
            win = self._windows.pop(machine_id, None)

        if win is None or not win["detections"]:
            return

        window_end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Get the order_id from the first detection (bridge already set it if available)
        order_id = win["detections"][0].get("order_id")

        batch = {
            "machine_id": machine_id,
            "order_id": order_id,
            "window_start": win["window_start"],
            "window_end": window_end,
            "detections": win["detections"],
        }

        logger.info(
            "Closing batch window for machine %s — %s detections",
            machine_id, len(win["detections"]),
        )

        # Spawn background thread for the actual LLM analysis
        # (analyze_batch itself is fast — just an INSERT — so we keep the
        #  heavy LLM work separate; the bridge caller can attach a real
        #  LLM background job later.)
        threading.Thread(
            target=self._run_analysis,
            args=(batch,),
            daemon=True,
        ).start()

    def _run_analysis(self, batch: dict[str, Any]) -> None:
        """Run analyze_batch in a background thread."""
        try:
            alert_id = analyze_batch(batch)
            logger.info("analyze_batch returned AlertID=%s", alert_id)
        except Exception as exc:
            logger.error("analyze_batch raised: %s", safe_log_text(exc))
