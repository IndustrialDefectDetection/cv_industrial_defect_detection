"""Render the demo burst the way the bridge sees it: every detection drawn,
but only the ones clearing the 0.80 gate marked as reaching the agent.

Offline - no server, no database, no API credit. Just the weights.
"""
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parents[1]
MLOPS = ROOT / "steel-defect-detection-mlops"
WEIGHTS = MLOPS / "runs/detect/steel_defect_colab_50_epochs/weights/best.pt"
BURST = MLOPS / "data/demo_burst"
OUT = ROOT / "docs/images/demo_burst_detections.jpg"

GATE = 0.80
PASS_COLOUR = (80, 220, 100)      # BGR - clears the gate, reaches the agent
BLOCK_COLOUR = (140, 140, 140)    # BGR - stored, but never wakes the agent
TILE = 320
PAD = 10
HEADER = 54
FOOTER = 46
BG = 24


def draw(image, result, names):
    image = cv2.resize(image, (TILE, TILE), interpolation=cv2.INTER_CUBIC)
    height, width = result.orig_shape
    scale_x, scale_y = TILE / width, TILE / height
    passed = 0

    boxes = sorted(result.boxes, key=lambda b: float(b.conf[0]))
    for box in boxes:
        confidence = float(box.conf[0])
        clears = confidence >= GATE
        passed += clears
        colour = PASS_COLOUR if clears else BLOCK_COLOUR
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        p1 = (int(x1 * scale_x), int(y1 * scale_y))
        p2 = (int(x2 * scale_x), int(y2 * scale_y))
        cv2.rectangle(image, p1, p2, colour, 2 if clears else 1)

        if clears:
            label = f"{names[int(box.cls[0])]} {confidence:.2f}"
            (text_width, text_height), _ = cv2.getTextSize(
                label, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
            top = max(p1[1] - text_height - 6, 0)
            cv2.rectangle(
                image,
                (p1[0], top),
                (p1[0] + text_width + 6, top + text_height + 6),
                colour,
                -1,
            )
            cv2.putText(
                image, label, (p1[0] + 3, top + text_height + 1),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (20, 20, 20), 1, cv2.LINE_AA)

    return image, len(boxes), passed


def main():
    model = YOLO(str(WEIGHTS))
    images = sorted(BURST.glob("*.jpg"))
    if not images:
        raise SystemExit(f"no burst images in {BURST}")

    tiles, total, cleared = [], 0, 0
    for path in images:
        result = model.predict(str(path), conf=0.25, verbose=False)[0]
        original = cv2.imread(str(path))
        tile, found, passed = draw(original, result, model.names)
        tiles.append(tile)
        total += found
        cleared += passed

    columns = len(tiles)
    width = columns * TILE + (columns + 1) * PAD
    canvas = np.full((HEADER + TILE + FOOTER, width, 3), BG, np.uint8)
    for index, tile in enumerate(tiles):
        x = PAD + index * (TILE + PAD)
        canvas[HEADER:HEADER + TILE, x:x + TILE] = tile

    cv2.putText(
        canvas, "One camera burst, as the bridge sees it",
        (PAD, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (240, 240, 240), 1,
        cv2.LINE_AA)

    baseline = HEADER + TILE + 29
    cv2.putText(
        canvas,
        f"{total} detections stored",
        (PAD, baseline), cv2.FONT_HERSHEY_SIMPLEX, 0.5, BLOCK_COLOUR, 1,
        cv2.LINE_AA)
    cv2.putText(
        canvas,
        f"{cleared} cleared the {GATE:.2f} gate and woke the agent",
        (PAD + 190, baseline), cv2.FONT_HERSHEY_SIMPLEX, 0.5, PASS_COLOUR, 1,
        cv2.LINE_AA)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(OUT), canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"{OUT}  {OUT.stat().st_size} bytes")
    print(f"{len(images)} images, {total} detections, {cleared} cleared {GATE}")


if __name__ == "__main__":
    main()
