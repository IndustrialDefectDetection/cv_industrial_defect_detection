"""Security tests for the standalone Streamlit image-upload boundary."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from streamlit_image_security import (
    UploadValidationError,
    decode_image_bytes,
    restore_safe_pillow_open,
    safe_uploaded_filename,
    validate_upload_batch,
)


class Upload:
    def __init__(self, name: str, content: bytes):
        self.name = name
        self.size = len(content)
        self._content = content

    def getvalue(self):
        return self._content


def encoded_image(image_format: str = "PNG", size=(2, 2)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color="white").save(buffer, format=image_format)
    return buffer.getvalue()


def test_decoder_accepts_bounded_png_and_materializes_rgb():
    decoded = decode_image_bytes(encoded_image())

    assert decoded.mode == "RGB"
    assert decoded.size == (2, 2)


def test_decoder_rejects_non_image_and_unsupported_format():
    with pytest.raises(UploadValidationError, match="valid image"):
        decode_image_bytes(b"not an image")

    with pytest.raises(UploadValidationError, match="JPEG and PNG"):
        decode_image_bytes(encoded_image("GIF"))


def test_ultralytics_decoder_wrapper_can_be_restored(monkeypatch):
    patched_open = lambda *_args, **_kwargs: None
    monkeypatch.setattr(Image, "open", patched_open)

    restore_safe_pillow_open()

    assert Image.open is not patched_open
    with pytest.raises(UploadValidationError, match="valid image"):
        decode_image_bytes(b"not an image")


def test_decoder_rejects_excessive_dimensions_before_inference():
    with pytest.raises(UploadValidationError, match="dimensions"):
        decode_image_bytes(encoded_image(size=(3, 2)), max_pixels=4)


def test_batch_bounds_count_and_total_bytes():
    uploads = [Upload(f"{index}.png", b"abc") for index in range(3)]

    with pytest.raises(UploadValidationError, match="at most 2"):
        validate_upload_batch(uploads, max_files=2)
    with pytest.raises(UploadValidationError, match="combined"):
        validate_upload_batch(uploads, max_total_bytes=8)


def test_declared_oversized_upload_is_rejected_before_copy():
    upload = Upload("large.png", b"small")
    upload.size = 11 * 1024 * 1024
    upload.getvalue = lambda: pytest.fail("oversized buffer must not be copied")

    with pytest.raises(UploadValidationError, match="too large"):
        validate_upload_batch([upload], max_total_bytes=1_000)


def test_uploaded_filename_is_inert_for_csv_and_ui_output():
    assert safe_uploaded_filename("../../=HYPERLINK(\"bad\")\n.png") == (
        "upload_HYPERLINK__bad___.png"
    )


def test_direct_streamlit_invocations_bind_only_to_loopback():
    project_root = Path(__file__).resolve().parent.parent
    config = (project_root / ".streamlit" / "config.toml").read_text(
        encoding="utf-8"
    )

    assert 'address = "127.0.0.1"' in config
    assert "enableXsrfProtection = true" in config
