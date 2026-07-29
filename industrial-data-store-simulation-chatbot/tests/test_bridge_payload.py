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

from bridge.bridge import BBox, DetectionItem, DetectionPayload

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
