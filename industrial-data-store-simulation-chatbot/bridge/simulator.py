"""
Simulator — CONTRACTS.md STEP 2 pipeline.

1. Picks a random Frame Welding machine from the lookup helpers.
2. Loads images from a directory (or uses a sample image repeatedly).
3. POSTs each image to the inference API (port 8080 `/predict`).
4. Builds the §2 payload envelope from the response.
5. POSTs to the bridge (port 8081 `/detection`).

Usage:
    python -m bridge.simulator --image-dir /path/to/images [--interval 5]
"""

import argparse
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from bridge.mes_lookups import get_frame_machines

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("simulator")

INFERENCE_URL = "http://localhost:8080/predict"
BRIDGE_URL = "http://localhost:8081/detection"


def pick_machine():
    """Return a random Frame Welding machine dict, or None."""
    machines = get_frame_machines()
    if not machines:
        logger.warning("No Frame Welding machines found — check DB/connection.")
        return None
    chosen = random.choice(machines)
    logger.info("Picked machine %s (%s)", chosen["machineid"], chosen.get("name", ""))
    return chosen


def run_inference(image_path: str) -> dict | None:
    """POST an image to the inference API and return the parsed response."""
    try:
        with open(image_path, "rb") as f:
            files = {"file": (Path(image_path).name, f, "image/jpeg")}
            resp = requests.post(INFERENCE_URL, files=files, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("Inference call failed for %s: %s", image_path, exc)
        return None


def build_payload(inference_result: dict, machine_id: int) -> dict:
    """Build the CONTRACTS.md §2 payload from the inference API result."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    detections = []
    for det in inference_result.get("detections", []):
        detections.append({
            "class": det["class"],
            "class_id": det["class_id"],
            "confidence": det["confidence"],
            "bbox": det["bbox"],  # {x1, y1, x2, y2}
        })

    return {
        "timestamp": timestamp,
        "machine_id": machine_id,
        "image_name": inference_result.get("image_name", "unknown.jpg"),
        "inference_time_ms": inference_result.get("inference_time_ms", 0.0),
        "detections": detections,
    }


def send_to_bridge(payload: dict) -> bool:
    """POST the §2 payload to the bridge's /detection endpoint."""
    try:
        resp = requests.post(BRIDGE_URL, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info(
            "Bridge accepted payload: %s",
            resp.json(),
        )
        return True
    except Exception as exc:
        logger.error("Bridge POST failed: %s", exc)
        return False


def main():
    parser = argparse.ArgumentParser(description="CV → Bridge simulator")
    parser.add_argument(
        "--image-dir",
        default=".",
        help="Directory containing images to process (default: cwd)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between image captures (default: 5.0)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Continuously loop through images",
    )
    args = parser.parse_args()

    image_dir = Path(args.image_dir)
    if not image_dir.is_dir():
        logger.error("Image directory not found: %s", image_dir)
        sys.exit(1)

    # Gather image files
    extensions = {".jpg", ".jpeg", ".png", ".bmp"}
    image_files = sorted(
        p for p in image_dir.iterdir() if p.suffix.lower() in extensions
    )
    if not image_files:
        logger.error("No image files found in %s", image_dir)
        sys.exit(1)

    logger.info("Found %s image(s) in %s", len(image_files), image_dir)

    # Pick a machine once (or could rotate per-image)
    machine = pick_machine()
    if machine is None:
        sys.exit(1)
    machine_id = machine["machineid"]

    idx = 0
    while True:
        image_path = str(image_files[idx % len(image_files)])

        logger.info("Processing image %s ...", image_path)
        inference_result = run_inference(image_path)
        if inference_result is None or not inference_result.get("success"):
            logger.warning("Skipping image (inference failed or no detections)")
        else:
            payload = build_payload(inference_result, machine_id)
            logger.info(
                "Sending payload: %s detections, machine=%s",
                len(payload["detections"]),
                machine_id,
            )
            send_to_bridge(payload)

        idx += 1

        if not args.loop and idx >= len(image_files):
            break

        time.sleep(args.interval)


if __name__ == "__main__":
    main()