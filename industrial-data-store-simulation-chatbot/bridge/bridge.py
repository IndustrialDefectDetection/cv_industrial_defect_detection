"""Authenticated, bounded camera-to-MES detection bridge (CONTRACTS.md)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
import random
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import psycopg2
import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from bridge.batch_manager import BatchManager
from bridge.db_config import (
    CONF_GATE,
    connection_kwargs,
)
from app_factory.shared.display_security import safe_log_text
from bridge.mes_lookups import get_active_work_order, get_frame_machines

logger = logging.getLogger(__name__)


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


MAX_REQUEST_BYTES = _bounded_int_env(
    "MES_BRIDGE_MAX_REQUEST_BYTES",
    1024 * 1024,
    1024,
    1024 * 1024,
)
DEFECT_CLASSES = (
    "crazing",
    "inclusion",
    "patches",
    "pitted_surface",
    "rolled-in_scale",
    "scratches",
)
CLASS_IDS = {name: class_id for class_id, name in enumerate(DEFECT_CLASSES)}
SAFE_IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

# The demo burst replays a fixed, committed folder through the real camera
# path. The directory is resolved here and never taken from the caller: a
# request-supplied path would turn this into an arbitrary-file reader that
# posts whatever it finds to the inference API.
DEMO_BURST_DIR = (
    Path(__file__).resolve().parents[2]
    / "steel-defect-detection-mlops" / "data" / "demo_burst"
)
DEMO_BURST_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
INFERENCE_URL = "http://127.0.0.1:8080/predict"

app = FastAPI(title="Defect Detection Bridge", version="1.2.0")
batch_manager = BatchManager()
_DB_SLOTS = threading.BoundedSemaphore(
    _bounded_int_env("MES_BRIDGE_DB_CONCURRENCY", 4, 1, 4)
)
# One burst at a time. A burst can end in a paid agent run, and two overlapping
# bursts would land in the same batch window and be indistinguishable anyway.
_BURST_LOCK = threading.Lock()


def require_internal_api_token(
    supplied: Annotated[str | None, Header(alias="X-MES-Internal-Token")] = None,
) -> None:
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


@contextlib.contextmanager
def _held_database_slot():
    """The body of `database_slot`, for callers that are not route dependencies."""
    if not _DB_SLOTS.acquire(timeout=2):
        raise HTTPException(
            status_code=429,
            detail="Bridge is busy",
            headers={"Retry-After": "2"},
        )
    try:
        yield
    finally:
        _DB_SLOTS.release()


def database_slot():
    """Bound concurrent remote DB work without blocking the event loop."""
    if not _DB_SLOTS.acquire(timeout=2):
        raise HTTPException(
            status_code=429,
            detail="Bridge is busy",
            headers={"Retry-After": "2"},
        )
    try:
        yield
    finally:
        _DB_SLOTS.release()


class InternalBoundaryMiddleware:
    """Authenticate and count bytes before FastAPI parses detection JSON."""

    _PUBLIC_PATHS = {
        "/health",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }

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
        """Keep authenticated detection data out of intermediary caches."""
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

        headers = dict(scope.get("headers", []))
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
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

        buffered_messages = []
        received = 0
        deadline = asyncio.get_running_loop().time() + 10
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


app.add_middleware(
    InternalBoundaryMiddleware,
    max_body_bytes=MAX_REQUEST_BYTES,
)


class BBox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x1: StrictFloat = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    y1: StrictFloat = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    x2: StrictFloat = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    y2: StrictFloat = Field(ge=0, le=1_000_000, allow_inf_nan=False)

    @model_validator(mode="after")
    def coordinates_are_ordered(self):
        if self.x2 < self.x1 or self.y2 < self.y1:
            raise ValueError("bbox maximums must be greater than or equal to minimums")
        return self


class DetectionItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    class_: Literal[
        "crazing",
        "inclusion",
        "patches",
        "pitted_surface",
        "rolled-in_scale",
        "scratches",
    ] = Field(alias="class")
    class_id: StrictInt = Field(ge=0, le=5)
    confidence: StrictFloat = Field(ge=0, le=1, allow_inf_nan=False)
    bbox: BBox

    @model_validator(mode="after")
    def class_matches_id(self):
        if CLASS_IDS[self.class_] != self.class_id:
            raise ValueError("class_id does not match class")
        return self


class DetectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    machine_id: StrictInt = Field(gt=0)
    image_name: str = Field(min_length=1, max_length=255)
    inference_time_ms: StrictFloat = Field(ge=0, le=300_000, allow_inf_nan=False)
    detections: list[DetectionItem] = Field(min_length=0, max_length=100)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @field_validator("image_name")
    @classmethod
    def image_name_must_not_be_a_path(cls, value: str) -> str:
        if Path(value).name != value or not SAFE_IMAGE_NAME.fullmatch(value):
            raise ValueError("image_name contains unsupported characters")
        return value


def _get_conn():
    return psycopg2.connect(**connection_kwargs())


@app.get("/health", include_in_schema=False)
def health():
    """Cheap readiness probe; DB readiness is exercised by authenticated writes."""
    ready = len(os.getenv("MES_INTERNAL_API_TOKEN", "")) >= 32
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "healthy" if ready else "unavailable"},
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post(
    "/detection",
    dependencies=[*INTERNAL_AUTH, Depends(database_slot)],
)
def handle_detection(payload: DetectionPayload):
    """Persist every valid detection, then gate committed rows into batching."""
    machines = get_frame_machines()
    if not machines:
        raise HTTPException(status_code=503, detail="MES machine lookup is unavailable")
    known_machine_ids = {row["machineid"] for row in machines}
    if payload.machine_id not in known_machine_ids:
        raise HTTPException(status_code=422, detail="Unknown Frame Welding machine")

    order = get_active_work_order(payload.machine_id)
    order_id = order["orderid"] if order else None
    payload_ts = payload.timestamp
    saved_ids: list[int] = []
    gated_records: list[dict] = []
    conn = None

    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            for det in payload.detections:
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
                        det.bbox.model_dump_json(),
                        payload.image_name,
                        payload.inference_time_ms,
                    ),
                )
                detection_id = cur.fetchone()[0]
                saved_ids.append(detection_id)
                if det.confidence >= CONF_GATE:
                    gated_records.append(
                        {
                            "machine_id": payload.machine_id,
                            "order_id": order_id,
                            "detection_id": detection_id,
                            "timestamp": payload_ts.isoformat(),
                            "class": det.class_,
                            "confidence": det.confidence,
                            "image_name": payload.image_name,
                        }
                    )
        conn.commit()
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        logger.error(
            "Failed to save detection batch: %s",
            safe_log_text(exc),
        )
        raise HTTPException(status_code=500, detail="Could not save detections") from exc
    finally:
        if conn is not None:
            conn.close()

    accepted_for_batch = sum(batch_manager.feed(item) for item in gated_records)
    if accepted_for_batch < len(gated_records):
        logger.warning(
            "Batch capacity discarded %s gated item(s) after persistence",
            len(gated_records) - accepted_for_batch,
        )

    logger.info(
        "Saved %s detection(s) from machine %s; %s entered batching",
        len(saved_ids),
        payload.machine_id,
        accepted_for_batch,
    )
    return {
        "saved_count": len(saved_ids),
        "detection_ids": saved_ids,
        "order_id": order_id,
        "batched_count": accepted_for_batch,
    }


def _internal_headers() -> dict[str, str]:
    return {"X-MES-Internal-Token": os.getenv("MES_INTERNAL_API_TOKEN", "")}


def _infer(image_path: Path) -> dict | None:
    """POST one image to the inference API, exactly as the simulator does."""
    try:
        media_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        with open(image_path, "rb") as handle:
            with requests.Session() as session:
                # Loopback only: never inherit a proxy, never follow a redirect
                # that could carry the internal token off this machine.
                session.trust_env = False
                response = session.post(
                    INFERENCE_URL,
                    files={"file": (image_path.name, handle, media_type)},
                    headers=_internal_headers(),
                    allow_redirects=False,
                    timeout=30,
                )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.error("Demo burst inference failed: %s", safe_log_text(exc))
        return None


@app.post("/simulate", dependencies=[*INTERNAL_AUTH])
def simulate_burst():
    """Replay the committed demo images through the real camera path.

    This exists so the trace dashboard can start a demo without a terminal.
    It is a thin driver, not a second pipeline: each image goes to the same
    inference API and then through `handle_detection`, so the confidence gate,
    the batching window and the alert lifecycle are the real ones. Nothing here
    is reachable without the internal token, and the image folder is fixed at
    import time rather than taken from the caller.
    """
    if not _BURST_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="A demo burst is already running",
            headers={"Retry-After": "15"},
        )
    try:
        if not DEMO_BURST_DIR.is_dir():
            raise HTTPException(
                status_code=503,
                detail="Demo images are not installed on this machine",
            )
        images = sorted(
            path for path in DEMO_BURST_DIR.iterdir()
            if path.suffix.lower() in DEMO_BURST_SUFFIXES and path.is_file()
        )
        if not images:
            raise HTTPException(status_code=503, detail="No demo images to send")

        machines = get_frame_machines()
        if not machines:
            raise HTTPException(
                status_code=503,
                detail="MES machine lookup is unavailable",
            )
        machine = random.choice(machines)

        saved = 0
        batched = 0
        for image in images:
            result = _infer(image)
            if not result or not result.get("success"):
                continue
            payload = DetectionPayload(
                timestamp=datetime.now(timezone.utc),
                machine_id=machine["machineid"],
                image_name=result.get("image_name", image.name),
                inference_time_ms=float(result.get("inference_time_ms", 0.0)),
                detections=result.get("detections", []),
            )
            with _held_database_slot():
                outcome = handle_detection(payload)
            saved += outcome["saved_count"]
            batched += outcome["batched_count"]

        logger.info(
            "Demo burst: %s image(s), %s detection(s) saved, %s gated",
            len(images),
            saved,
            batched,
        )
        return {
            "images_sent": len(images),
            "saved_count": saved,
            "batched_count": batched,
            "machine_id": machine["machineid"],
            "machine_name": machine.get("name", ""),
        }
    finally:
        _BURST_LOCK.release()
