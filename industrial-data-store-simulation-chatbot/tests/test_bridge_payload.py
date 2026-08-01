"""The bridge must accept exactly the payload the camera sends.

Regression test for a bug that made the whole CV pipeline a no-op: "class"
is a Python keyword, so DetectionItem names the attribute class_, and without
an explicit alias Pydantic looked for a literal "class_" key in the JSON. Every
payload the simulator sent was rejected with 422, no VisionDetections row was
ever written, and no alert was ever raised - while both services reported
healthy and the inference API happily returned detections.

These tests are pure schema checks: no database, no network.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bridge.bridge import (
    BBox,
    DetectionItem,
    DetectionPayload,
    require_internal_api_token,
    app,
)
from bridge import mes_lookups

# Verbatim from CONTRACTS.md §2.
CONTRACT_PAYLOAD = {
    "timestamp": "2026-07-19T14:02:31Z",
    "machine_id": 12,
    "image_name": "scratches_101.jpg",
    "inference_time_ms": 42.1,
    "detections": [
        {
            "class": "scratches",
            "class_id": 5,
            "confidence": 0.8712,
            "bbox": {"x1": 10.5, "y1": 20.0, "x2": 88.2, "y2": 95.1},
        }
    ],
}


def test_the_contract_payload_is_accepted():
    payload = DetectionPayload.model_validate(CONTRACT_PAYLOAD)

    assert payload.machine_id == 12
    detection = payload.detections[0]
    assert detection.class_ == "scratches"
    assert detection.class_id == 5
    assert detection.confidence == pytest.approx(0.8712)
    assert detection.bbox.x2 == pytest.approx(88.2)


def test_the_defect_class_survives_the_keyword_rename():
    """The whole bug in one line: JSON says "class", the attribute is class_."""
    detection = DetectionItem.model_validate({
        "class": "pitted_surface", "class_id": 3, "confidence": 0.92,
        "bbox": {"x1": 0.0, "y1": 0.0, "x2": 199.4, "y2": 199.7},
    })

    assert detection.class_ == "pitted_surface"


def test_construction_by_attribute_name_still_works():
    """populate_by_name keeps hand-built items usable in other tests."""
    detection = DetectionItem(
        class_="patches", class_id=2, confidence=0.86,
        bbox=BBox(x1=0, y1=0, x2=10, y2=10),
    )

    assert detection.class_ == "patches"


def test_a_payload_without_a_class_is_still_rejected():
    """The alias must not turn the field optional."""
    with pytest.raises(Exception):
        DetectionItem.model_validate(
            {"class_id": 5, "confidence": 0.9,
             "bbox": {"x1": 0, "y1": 0, "x2": 1, "y2": 1}})


def test_every_yolo_class_name_round_trips():
    """The six NEU-DET classes the inference API can emit (CLASS_NAMES)."""
    for index, name in enumerate(["crazing", "inclusion", "patches",
                                  "pitted_surface", "rolled-in_scale",
                                  "scratches"]):
        detection = DetectionItem.model_validate({
            "class": name, "class_id": index, "confidence": 0.9,
            "bbox": {"x1": 0, "y1": 0, "x2": 1, "y2": 1},
        })
        assert detection.class_ == name


def test_payload_rejects_paths_extra_fields_and_mismatched_classes():
    bad_path = {**CONTRACT_PAYLOAD, "image_name": "../secret.jpg"}
    with pytest.raises(ValidationError):
        DetectionPayload.model_validate(bad_path)

    extra = {**CONTRACT_PAYLOAD, "unexpected": True}
    with pytest.raises(ValidationError):
        DetectionPayload.model_validate(extra)

    mismatch = {
        **CONTRACT_PAYLOAD["detections"][0],
        "class": "scratches",
        "class_id": 0,
    }
    with pytest.raises(ValidationError):
        DetectionItem.model_validate(mismatch)


def test_payload_rejects_unbounded_or_invalid_geometry():
    too_many = {
        **CONTRACT_PAYLOAD,
        "detections": CONTRACT_PAYLOAD["detections"] * 101,
    }
    with pytest.raises(ValidationError):
        DetectionPayload.model_validate(too_many)

    with pytest.raises(ValidationError):
        BBox(x1=10.0, y1=0.0, x2=1.0, y2=1.0)


def test_internal_service_auth_fails_closed(monkeypatch):
    monkeypatch.delenv("MES_INTERNAL_API_TOKEN", raising=False)
    with pytest.raises(HTTPException) as missing:
        require_internal_api_token(None)
    assert missing.value.status_code == 503

    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", "a" * 32)
    with pytest.raises(HTTPException) as wrong:
        require_internal_api_token("b" * 32)
    assert wrong.value.status_code == 401
    require_internal_api_token("a" * 32)


def test_bridge_authenticates_before_json_parsing(monkeypatch):
    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", "a" * 32)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/detection", content=b"{not-json")

    assert response.status_code == 401


def test_bridge_counts_actual_body_bytes(monkeypatch):
    token = "a" * 32
    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", token)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/detection",
        content=b"x" * (1024 * 1024 + 1),
        headers={
            "Content-Length": "1",
            "X-MES-Internal-Token": token,
        },
    )

    assert response.status_code == 413


def test_bridge_responses_disable_caching(monkeypatch):
    token = "a" * 32
    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", token)
    client = TestClient(app, raise_server_exceptions=False)

    health_response = client.get("/health")
    assert health_response.headers["cache-control"] == "no-store"
    assert health_response.headers["x-content-type-options"] == "nosniff"

    protected_response = client.post(
        "/detection",
        content=b"{not-json",
        headers={
            "Content-Type": "application/json",
            "X-MES-Internal-Token": token,
        },
    )
    assert protected_response.status_code == 422
    assert protected_response.headers["cache-control"] == "private, no-store"
    assert protected_response.headers["x-content-type-options"] == "nosniff"
    assert protected_response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    ("lookup", "arguments", "expected"),
    [
        (mes_lookups.get_frame_machines, (), []),
        (mes_lookups.get_active_work_order, (12,), None),
    ],
)
def test_lookup_failures_close_database_connections(
    monkeypatch,
    lookup,
    arguments,
    expected,
):
    class BrokenCursor:
        def __enter__(self):
            raise RuntimeError("query failed")

        def __exit__(self, *_args):
            return False

    class Connection:
        closed = False

        def cursor(self, **_kwargs):
            return BrokenCursor()

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(mes_lookups, "_get_conn", lambda: connection)

    assert lookup(*arguments) == expected
    assert connection.closed
