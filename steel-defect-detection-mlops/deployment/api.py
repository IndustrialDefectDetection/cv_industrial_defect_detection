"""FastAPI service for bounded, authenticated steel-defect inference."""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import re
import secrets
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image, UnidentifiedImageError

# Ultralytics monkeypatches Image.open with an auto-installing HEIF fallback.
# Keep Pillow's original decoder so malformed public input cannot trigger
# dependency installation or network work.
_PILLOW_OPEN = Image.open

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from ultralytics import YOLO
from deployment.model_integrity import verify_model_integrity

LOGGER = logging.getLogger(__name__)


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


MAX_IMAGE_BYTES = _bounded_int_env(
    "MES_MAX_IMAGE_BYTES", 10 * 1024 * 1024, 1024, 10 * 1024 * 1024
)
MAX_IMAGE_PIXELS = _bounded_int_env(
    "MES_MAX_IMAGE_PIXELS", 16_000_000, 1, 16_000_000
)
MAX_BATCH_FILES = _bounded_int_env("MES_MAX_BATCH_FILES", 16, 1, 16)
MAX_BATCH_BYTES = _bounded_int_env(
    "MES_MAX_BATCH_BYTES", 32 * 1024 * 1024, 1024, 32 * 1024 * 1024
)
MAX_REQUEST_BYTES = MAX_BATCH_BYTES + (1024 * 1024)
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG"}
_INFERENCE_SLOTS = asyncio.Semaphore(
    _bounded_int_env("MES_INFERENCE_CONCURRENCY", 1, 1, 4)
)

app = FastAPI(
    title="Steel Defect Detection API",
    description="YOLOv8-based steel surface defect detection service",
    version="1.1.0",
)


REQUEST_COUNT = Counter(
    "api_requests_total", "Total API requests", ["endpoint", "method"]
)
INFERENCE_TIME = Histogram(
    "inference_duration_seconds", "Inference time", ["model"]
)
DETECTION_COUNT = Counter(
    "detections_total", "Total defects detected", ["class"]
)

_DEFAULT_MODEL_PATH = str(
    Path(__file__).parent.parent
    / "runs/detect/steel_defect_colab_50_epochs/weights/best.pt"
)
# A blank MODEL_PATH means "not configured", not "load the file at ''".
# os.getenv's default only applies when the name is absent, so an empty
# assignment in .env - which is how an unused optional setting is usually
# written - would otherwise beat the default and fail startup with "the
# configured model file does not exist" while best.pt sits where it belongs.
MODEL_PATH = os.getenv("MODEL_PATH", "").strip() or _DEFAULT_MODEL_PATH
model = None

CLASS_NAMES = {
    0: "crazing",
    1: "inclusion",
    2: "patches",
    3: "pitted_surface",
    4: "rolled-in_scale",
    5: "scratches",
}


def require_internal_api_token(
    supplied: Annotated[str | None, Header(alias="X-MES-Internal-Token")] = None,
) -> None:
    """Fail closed unless the caller presents the shared service token."""
    configured = os.getenv("MES_INTERNAL_API_TOKEN", "")
    if len(configured) < 32:
        raise HTTPException(status_code=503, detail="Service authentication is not configured")
    if supplied is None or not secrets.compare_digest(supplied, configured):
        raise HTTPException(
            status_code=401,
            detail="Invalid service credentials",
            headers={"WWW-Authenticate": "MES-Internal"},
        )


INTERNAL_AUTH = [Depends(require_internal_api_token)]


class InternalBoundaryMiddleware:
    """Authenticate, bound, and serialize inference requests before parsing."""

    _PUBLIC_PATHS = {
        "/",
        "/health",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
    _INFERENCE_PATHS = {"/predict", "/batch-predict"}

    def __init__(self, app, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def _respond(self, scope, receive, send, code: int, detail: str):
        await JSONResponse(
            status_code=code,
            content={"detail": detail},
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )(scope, receive, send)

    async def _send_protected_response(self, message, send):
        """Keep authenticated inference output out of intermediary caches."""
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            header_names = {name.lower() for name, _value in headers}
            if b"cache-control" not in header_names:
                headers.append((b"cache-control", b"private, no-store"))
            if b"x-content-type-options" not in header_names:
                headers.append((b"x-content-type-options", b"nosniff"))
            if b"referrer-policy" not in header_names:
                headers.append((b"referrer-policy", b"no-referrer"))
            message = {**message, "headers": headers}
        await send(message)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        headers = dict(scope.get("headers", []))
        if path in self._PUBLIC_PATHS:
            if method not in {"GET", "HEAD"}:
                await self._respond(scope, receive, send, 405, "Method not allowed")
                return
            await self.app(scope, receive, send)
            return
        else:
            configured = os.getenv("MES_INTERNAL_API_TOKEN", "")
            supplied = headers.get(b"x-mes-internal-token")
            if len(configured) < 32:
                await self._respond(
                    scope,
                    receive,
                    send,
                    503,
                    "Service authentication is not configured",
                )
                return
            if supplied is None or not secrets.compare_digest(
                supplied, configured.encode("utf-8")
            ):
                await self._respond(
                    scope, receive, send, 401, "Invalid service credentials"
                )
                return

        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                declared_length = int(declared)
            except ValueError:
                await self._respond(
                    scope, receive, send, 400, "Invalid Content-Length"
                )
                return
            if declared_length < 0:
                await self._respond(
                    scope, receive, send, 400, "Invalid Content-Length"
                )
                return
            if declared_length > self.max_body_bytes:
                await self._respond(
                    scope, receive, send, 413, "Request body is too large"
                )
                return

        slot_acquired = False
        if path in self._INFERENCE_PATHS:
            try:
                await asyncio.wait_for(_INFERENCE_SLOTS.acquire(), timeout=5)
                slot_acquired = True
            except asyncio.TimeoutError:
                await self._respond(
                    scope, receive, send, 429, "Inference service is busy"
                )
                return

        try:
            buffered_messages = []
            received = 0
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await self._respond(
                        scope, receive, send, 408, "Request body timed out"
                    )
                    return
                try:
                    message = await asyncio.wait_for(receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    await self._respond(
                        scope, receive, send, 408, "Request body timed out"
                    )
                    return
                buffered_messages.append(message)
                if message["type"] == "http.disconnect":
                    break
                if message["type"] != "http.request":
                    continue
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    await self._respond(
                        scope, receive, send, 413, "Request body is too large"
                    )
                    return
                if not message.get("more_body", False):
                    break

            next_message = 0

            async def replay_receive():
                nonlocal next_message
                if next_message < len(buffered_messages):
                    message = buffered_messages[next_message]
                    next_message += 1
                    return message
                return await receive()

            async def protected_send(message):
                await self._send_protected_response(message, send)

            await self.app(scope, replay_receive, protected_send)
        finally:
            if slot_acquired:
                _INFERENCE_SLOTS.release()


app.add_middleware(
    InternalBoundaryMiddleware,
    max_body_bytes=MAX_REQUEST_BYTES,
)


@app.on_event("startup")
async def load_model() -> None:
    """Load YOLOv8 model on startup, then run one throwaway inference.

    `YOLO(path)` only reads the weights. Ultralytics defers the rest of its
    setup to the first `predict` call, so that call took 1.2-2.9 seconds while
    every later one took 40-60 ms. That number is reported to the bridge as
    `inference_time_ms` and stored per detection, and the agent twice read the
    resulting outlier as evidence of a failing camera and wrote it into a
    root-cause report as a HIGH-certainty finding.

    Warming the model here costs about a second of startup and means no
    request a caller makes is ever the first one. A failure to warm is logged
    but not fatal: the model is loaded and serving a slow first request beats
    refusing to start.
    """
    global model
    try:
        verified_model_path = verify_model_integrity(MODEL_PATH)
        model = YOLO(str(verified_model_path))
        LOGGER.info("Inference model loaded")
    except Exception:
        LOGGER.exception("Failed to load the inference model")
        raise

    try:
        blank = Image.new("RGB", (640, 640))
        started = time.monotonic()
        await asyncio.to_thread(
            lambda: model.predict(source=blank, save=False, verbose=False)
        )
        LOGGER.info(
            "Inference model warmed in %.0f ms; first real request will not "
            "carry the cold-start cost",
            (time.monotonic() - started) * 1000,
        )
    except Exception:
        LOGGER.warning(
            "Model warm-up failed; the first request will be slow and its "
            "inference_time_ms should not be read as a process anomaly",
            exc_info=True,
        )


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "Steel Defect Detection API", "status": "running"}


@app.get("/health", include_in_schema=False)
async def health_check():
    REQUEST_COUNT.labels(endpoint="/health", method="GET").inc()
    ready = (
        model is not None
        and len(os.getenv("MES_INTERNAL_API_TOKEN", "")) >= 32
    )
    status_code = 200 if ready else 503
    return Response(
        content='{"status":"healthy"}' if status_code == 200 else '{"status":"unavailable"}',
        status_code=status_code,
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/metrics", dependencies=INTERNAL_AUTH)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def safe_filename(filename: str | None) -> str:
    """Return conservative text safe to carry into the downstream prompt."""
    name = Path(filename or "upload").name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:255]
    if not name or not name[0].isalnum():
        name = f"upload_{name.lstrip('._-')}"[:255]
    return name or "upload"


async def read_image(file: UploadFile, remaining_bytes: int = MAX_IMAGE_BYTES):
    """Read, decode, and fully materialize a bounded JPEG or PNG upload."""
    limit = min(MAX_IMAGE_BYTES, remaining_bytes)
    if limit <= 0:
        raise HTTPException(status_code=413, detail="Batch upload is too large")

    try:
        image_bytes = await file.read(limit + 1)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid image data") from exc
    if len(image_bytes) > limit:
        raise HTTPException(status_code=413, detail="Image upload is too large")

    try:
        with _PILLOW_OPEN(io.BytesIO(image_bytes)) as opened:
            if opened.format not in ALLOWED_IMAGE_FORMATS:
                raise HTTPException(
                    status_code=415, detail="Only JPEG and PNG images are supported"
                )
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail="Image dimensions are too large")
            opened.load()
            image = opened.convert("RGB")
    except HTTPException:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=400, detail="Invalid image data") from exc
    except Exception as exc:
        LOGGER.warning("Unexpected image decoder failure", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid image data") from exc

    return image, len(image_bytes)


def parse_detections(result, width: int, height: int, include_class_id: bool = True):
    """Convert model output to finite, image-bounded response values."""
    detections = []
    for box in result.boxes:
        class_id = int(box.cls[0])
        class_name = CLASS_NAMES.get(class_id)
        confidence = float(box.conf[0])
        coordinates = [float(value) for value in box.xyxy[0].tolist()]
        if (
            class_name is None
            or not math.isfinite(confidence)
            or len(coordinates) != 4
            or not all(math.isfinite(value) for value in coordinates)
        ):
            continue

        x1, y1, x2, y2 = coordinates
        x1 = max(0.0, min(x1, float(width)))
        x2 = max(x1, min(x2, float(width)))
        y1 = max(0.0, min(y1, float(height)))
        y2 = max(y1, min(y2, float(height)))
        detection = {
            "class": class_name,
            "confidence": round(max(0.0, min(confidence, 1.0)), 4),
            "bbox": {
                "x1": round(x1, 2),
                "y1": round(y1, 2),
                "x2": round(x2, 2),
                "y2": round(y2, 2),
            },
        }
        if include_class_id:
            detection["class_id"] = class_id
        detections.append(detection)
        DETECTION_COUNT.labels(class_name).inc()
    return detections


async def infer(image: Image.Image, confidence: float):
    """Run one model call; middleware already owns the bounded request slot."""
    start = time.monotonic()
    result = await asyncio.to_thread(
        lambda: model.predict(
            source=image, conf=confidence, save=False, verbose=False
        )[0]
    )
    elapsed = time.monotonic() - start
    INFERENCE_TIME.labels(model="YOLOv8n").observe(elapsed)
    return result, elapsed * 1000


@app.post("/predict", dependencies=INTERNAL_AUTH)
async def predict_defects(
    file: Annotated[UploadFile, File(...)],
    confidence: Annotated[float, Query(ge=0.0, le=1.0)] = 0.25,
):
    if model is None:
        raise HTTPException(status_code=503, detail="Model is unavailable")

    REQUEST_COUNT.labels(endpoint="/predict", method="POST").inc()
    image, _ = await read_image(file)
    try:
        result, inference_ms = await infer(image, confidence)
        detections = parse_detections(result, image.width, image.height)
        return {
            "success": True,
            "image_name": safe_filename(file.filename),
            "image_size": {"width": image.width, "height": image.height},
            "num_detections": len(detections),
            "detections": detections,
            "inference_time_ms": round(inference_ms, 2),
            "model": "YOLOv8n",
            "confidence_threshold": confidence,
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("Inference request failed")
        raise HTTPException(status_code=500, detail="Inference failed") from exc
    finally:
        image.close()


@app.post("/batch-predict", dependencies=INTERNAL_AUTH)
async def batch_predict_defects(
    files: Annotated[list[UploadFile], File(..., min_length=1, max_length=MAX_BATCH_FILES)],
    confidence: Annotated[float, Query(ge=0.0, le=1.0)] = 0.25,
):
    if model is None:
        raise HTTPException(status_code=503, detail="Model is unavailable")

    REQUEST_COUNT.labels(endpoint="/batch-predict", method="POST").inc()
    results_list = []
    bytes_read = 0
    for file in files:
        image = None
        try:
            image, image_bytes = await read_image(file, MAX_BATCH_BYTES - bytes_read)
            bytes_read += image_bytes
            result, inference_ms = await infer(image, confidence)
            detections = parse_detections(
                result, image.width, image.height, include_class_id=False
            )
            results_list.append(
                {
                    "filename": safe_filename(file.filename),
                    "num_detections": len(detections),
                    "detections": detections,
                    "inference_time_ms": round(inference_ms, 2),
                }
            )
        except HTTPException:
            raise
        except Exception:
            LOGGER.exception("Batch inference item failed")
            results_list.append(
                {
                    "filename": safe_filename(file.filename),
                    "error": "Inference failed",
                }
            )
        finally:
            if image is not None:
                image.close()

    return {
        "success": True,
        "total_images": len(files),
        "results": results_list,
    }


@app.get("/model-info", dependencies=INTERNAL_AUTH)
async def model_info():
    if model is None:
        raise HTTPException(status_code=503, detail="Model is unavailable")

    return {
        "model_name": "YOLOv8n",
        "num_classes": 6,
        "classes": CLASS_NAMES,
        "input_size": "640x640",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)
