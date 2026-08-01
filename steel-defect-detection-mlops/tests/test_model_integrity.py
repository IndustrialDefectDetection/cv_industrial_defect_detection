"""Tests for the executable model-artifact trust boundary."""

import hashlib
from pathlib import Path

import pytest

from deployment.model_integrity import verify_model_integrity


def test_model_integrity_accepts_only_the_expected_bytes(tmp_path):
    model_path = tmp_path / "model.pt"
    model_path.write_bytes(b"reviewed model bytes")
    expected = hashlib.sha256(model_path.read_bytes()).hexdigest()

    assert verify_model_integrity(model_path, expected) == model_path

    with pytest.raises(RuntimeError, match="integrity"):
        verify_model_integrity(model_path, "0" * 64)


def test_model_integrity_rejects_invalid_digest_configuration(tmp_path):
    model_path = tmp_path / "model.pt"
    model_path.write_bytes(b"bytes")

    with pytest.raises(RuntimeError, match="MODEL_SHA256"):
        verify_model_integrity(model_path, "not-a-digest")


def test_api_container_points_at_the_copied_model_artifact():
    project_root = Path(__file__).resolve().parent.parent
    dockerfile = (project_root / "deployment" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "MODEL_PATH=/app/models/best.pt" in dockerfile
    assert "/app/models/best.pt" in dockerfile
