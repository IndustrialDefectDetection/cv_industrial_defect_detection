"""Bounded image decoding helpers for the standalone Streamlit UI."""

from __future__ import annotations

import io
import os
import re
import sys
from typing import Iterable

from PIL import Image, UnidentifiedImageError

# Capture Pillow's decoder before Ultralytics is imported. Ultralytics can
# replace Image.open with a wrapper that attempts dependency installation for
# some formats, which must never be reachable from an untrusted upload.
_ultralytics_patches = sys.modules.get("ultralytics.utils.patches")
if (
    _ultralytics_patches is not None
    and Image.open is getattr(_ultralytics_patches, "image_open", None)
):
    # Stay safe even if this helper is imported after another module already
    # imported Ultralytics.
    _PILLOW_OPEN = _ultralytics_patches._image_open
else:
    _PILLOW_OPEN = Image.open


def restore_safe_pillow_open() -> None:
    """Undo Ultralytics' auto-installing decoder wrapper process-wide."""

    Image.open = _PILLOW_OPEN


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
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG"}


class UploadValidationError(ValueError):
    """Raised with a user-safe message when an upload crosses a boundary."""


def safe_uploaded_filename(filename: object) -> str:
    """Return a short inert basename suitable for UI and CSV output."""

    raw_name = "" if filename is None else str(filename)
    leaf_name = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", leaf_name)[:128]
    if not safe_name or not safe_name[0].isalnum():
        safe_name = f"upload_{safe_name.lstrip('._-')}"[:128]
    return safe_name or "upload"


def decode_image_bytes(
    image_bytes: bytes,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> Image.Image:
    """Decode one fully bounded JPEG/PNG into a materialized RGB image."""

    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise UploadValidationError("The uploaded file is not a valid image.")
    if len(image_bytes) > max_bytes:
        raise UploadValidationError("The uploaded image is too large.")

    try:
        with _PILLOW_OPEN(io.BytesIO(image_bytes)) as opened:
            if opened.format not in ALLOWED_IMAGE_FORMATS:
                raise UploadValidationError(
                    "Only JPEG and PNG images are supported."
                )
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise UploadValidationError("The image dimensions are too large.")
            opened.load()
            return opened.convert("RGB")
    except UploadValidationError:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
    ) as exc:
        raise UploadValidationError(
            "The uploaded file is not a valid image."
        ) from exc
    except Exception as exc:
        raise UploadValidationError(
            "The uploaded file could not be decoded safely."
        ) from exc


def read_uploaded_file(
    uploaded_file: object, *, max_bytes: int = MAX_IMAGE_BYTES
) -> bytes:
    """Reject an oversized Streamlit upload before copying its memory buffer."""

    declared_size = getattr(uploaded_file, "size", None)
    if (
        isinstance(declared_size, int)
        and (declared_size < 0 or declared_size > max_bytes)
    ):
        raise UploadValidationError("The uploaded image is too large.")

    getvalue = getattr(uploaded_file, "getvalue", None)
    if not callable(getvalue):
        raise UploadValidationError("An uploaded file could not be read safely.")
    file_bytes = getvalue()
    if not isinstance(file_bytes, bytes):
        raise UploadValidationError("An uploaded file could not be read safely.")
    if len(file_bytes) > max_bytes:
        raise UploadValidationError("The uploaded image is too large.")
    return file_bytes


def validate_upload_batch(
    uploaded_files: Iterable[object],
    *,
    max_files: int = MAX_BATCH_FILES,
    max_total_bytes: int = MAX_BATCH_BYTES,
) -> list[tuple[str, bytes]]:
    """Read a bounded upload collection and return inert names plus bytes."""

    files = list(uploaded_files)
    if len(files) > max_files:
        raise UploadValidationError(
            f"A batch can contain at most {max_files} images."
        )

    validated_uploads: list[tuple[str, bytes]] = []
    total_bytes = 0
    for uploaded_file in files:
        file_bytes = read_uploaded_file(uploaded_file)
        total_bytes += len(file_bytes)
        if total_bytes > max_total_bytes:
            raise UploadValidationError("The combined batch upload is too large.")
        validated_uploads.append(
            (
                safe_uploaded_filename(getattr(uploaded_file, "name", None)),
                file_bytes,
            )
        )
    return validated_uploads
