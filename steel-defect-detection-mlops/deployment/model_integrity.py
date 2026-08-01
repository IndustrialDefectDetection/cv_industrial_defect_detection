"""Integrity verification for the executable PyTorch model artifact."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path


# SHA-256 of the demo model documented in the root AGENTS.md (6,251,818 bytes).
# Updating/retraining the model must deliberately update this value and the
# matching deployment documentation in the same review.
DEFAULT_MODEL_SHA256 = (
    "de5c11747b2b85554b06c570dae9e50a1e8883c75db93d2eb7396895868d3a3e"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def verify_model_integrity(
    model_path: str | os.PathLike[str],
    expected_sha256: str | None = None,
) -> Path:
    """Return a model path only when its bytes match the trusted digest."""

    expected = (
        expected_sha256
        if expected_sha256 is not None
        else os.getenv("MODEL_SHA256", DEFAULT_MODEL_SHA256)
    ).strip().lower()
    if _SHA256_PATTERN.fullmatch(expected) is None:
        raise RuntimeError("MODEL_SHA256 must be a 64-character SHA-256 digest")

    path = Path(model_path)
    if not path.is_file():
        raise RuntimeError("The configured model file does not exist")

    digest = hashlib.sha256()
    try:
        with path.open("rb") as model_file:
            for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError("The configured model file could not be read") from exc

    if not secrets.compare_digest(digest.hexdigest(), expected):
        raise RuntimeError(
            "Model integrity verification failed; refuse to deserialize it"
        )
    return path
