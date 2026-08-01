"""Security-boundary tests for the internal agent API."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api
import report_paths


def test_internal_api_auth_fails_closed_without_configuration(monkeypatch):
    monkeypatch.delenv("MES_INTERNAL_API_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc:
        api.require_internal_api_token(None)

    assert exc.value.status_code == 503


def test_internal_api_auth_rejects_the_wrong_token(monkeypatch):
    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", "a" * 32)

    with pytest.raises(HTTPException) as exc:
        api.require_internal_api_token("b" * 32)

    assert exc.value.status_code == 401
    api.require_internal_api_token("a" * 32)


def test_middleware_authenticates_before_parsing_request_body(monkeypatch):
    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", "a" * 32)
    client = TestClient(api.app, raise_server_exceptions=False)

    response = client.post("/chat/", content=b"{not-json")

    assert response.status_code == 401


def test_middleware_counts_actual_body_bytes(monkeypatch):
    token = "a" * 32
    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", token)
    client = TestClient(api.app, raise_server_exceptions=False)

    response = client.post(
        "/chat/",
        content=b"x" * (1024 * 1024 + 1),
        headers={
            "Content-Length": "1",
            "X-MES-Internal-Token": token,
        },
    )

    assert response.status_code == 413


def test_only_health_is_public_and_protected_responses_are_not_cacheable(
    monkeypatch,
):
    token = "a" * 32
    monkeypatch.setenv("MES_INTERNAL_API_TOKEN", token)
    client = TestClient(api.app, raise_server_exceptions=False)

    assert client.get("/docs").status_code == 401
    assert client.get("/openapi.json").status_code == 401

    response = client.get(
        "/trace",
        headers={
            "X-MES-Internal-Token": token,
            "Origin": "https://attacker.invalid",
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "access-control-allow-origin" not in response.headers


def test_chat_input_is_bounded_and_cannot_be_blank():
    with pytest.raises(ValidationError):
        api.ChatRequest(
            conversation_id=uuid4(),
            user_input="x" * 4_001,
            history=[],
        )

    request = api.ChatRequest(
        conversation_id=uuid4(),
        user_input="   ",
        history=[],
    )
    previous = api._manager
    api._manager = object()
    try:
        with pytest.raises(HTTPException) as exc:
            api.send_message(request, user_id="user-1")
        assert exc.value.status_code == 400
    finally:
        api._manager = previous


def test_chat_history_contract_is_strict_and_bounded():
    conversation_id = uuid4()
    valid = api.ChatRequest(
        conversation_id=conversation_id,
        user_input="continue",
        history=[
            {"role": "user", "content": "maintenance correlation"},
            {"role": "assistant", "content": "Which machines?"},
        ],
    )
    assert valid.conversation_id == conversation_id

    with pytest.raises(ValidationError):
        api.ChatRequest(
            conversation_id=conversation_id,
            user_input="continue",
            history=[{"role": "assistant", "content": "orphaned answer"}],
        )

    with pytest.raises(ValidationError):
        api.ChatRequest(
            conversation_id=conversation_id,
            user_input="continue",
            history=[{"role": "user", "content": "dangling question"}],
        )

    with pytest.raises(ValidationError):
        api.ChatRequest(
            conversation_id=conversation_id,
            user_input="continue",
            history=[
                {"role": "user", "content": "first question"},
                {"role": "user", "content": "second question"},
            ],
        )

    with pytest.raises(ValidationError):
        api.ChatRequest(
            conversation_id=conversation_id,
            user_input="continue",
            history=[
                {"role": "user", "content": f"message {index}"}
                for index in range(api._MAX_CHAT_CONTEXT_MESSAGES + 1)
            ],
        )

    with pytest.raises(ValidationError):
        api.ChatRequest(
            conversation_id=conversation_id,
            user_input="continue",
            history=[
                {
                    "role": "user",
                    "content": "x" * (api._MAX_CHAT_CONTEXT_BYTES + 1),
                },
            ],
        )

    with pytest.raises(ValidationError):
        api.ChatRequest(
            conversation_id=conversation_id,
            user_input="continue",
            history=[
                {
                    "role": "user",
                    "content": "question",
                    "unexpected": "field",
                },
            ],
        )


def test_burst_payload_has_strict_counts_and_ranges():
    now = datetime.now(timezone.utc)
    detection = {
        "detection_id": 1,
        "timestamp": now,
        "class": "scratches",
        "confidence": 0.9,
        "image_name": "frame.jpg",
    }

    request = api.BurstRequest(
        machine_id=12,
        defect_type="scratches",
        detection_count=1,
        window_start=now,
        window_end=now,
        detections=[detection],
    )
    assert request.detections[0].class_ == "scratches"

    with pytest.raises(ValidationError):
        api.BurstRequest(
            machine_id=12,
            defect_type="not-a-real-class",
            detection_count=1,
            window_start=now,
            window_end=now,
            detections=[detection],
        )


def test_hourly_run_budget_returns_retry_after(monkeypatch):
    monkeypatch.setattr(api, "_MAX_RUNS_PER_HOUR", 1)
    with api._RUN_BUDGET_LOCK:
        api._RUN_STARTS.clear()

    api._reserve_run_budget()
    with pytest.raises(HTTPException) as exc:
        api._reserve_run_budget()

    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) >= 1

    with api._RUN_BUDGET_LOCK:
        api._RUN_STARTS.clear()


def test_health_does_not_disclose_paths_models_or_errors(monkeypatch):
    monkeypatch.setattr(api, "_manager", None)
    monkeypatch.setattr(api, "_manager_error", "sensitive backend detail")

    response = api.health()
    body = response.body.decode()

    assert response.status_code == 503
    assert "agent_manager_ready" in body
    assert "sensitive backend detail" not in body
    assert "db_path" not in body
    assert "model_id" not in body
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_report_endpoint_rejects_traversal_and_symlinks(tmp_path, monkeypatch):
    reports_directory = tmp_path / "reports"
    reports_directory.mkdir()
    safe_report = reports_directory / "safe.pdf"
    safe_report.write_bytes(b"%PDF-safe")
    outside_report = tmp_path / "outside.pdf"
    outside_report.write_bytes(b"%PDF-outside")
    linked_report = reports_directory / "linked.pdf"

    monkeypatch.setattr(report_paths, "REPORTS_DIR", reports_directory)

    response = api.get_report(safe_report.name)
    assert response.path == safe_report.resolve()

    for invalid_name in ("../safe.pdf", "not-a-report.txt"):
        with pytest.raises(HTTPException) as exc:
            api.get_report(invalid_name)
        assert exc.value.status_code == 404

    try:
        linked_report.symlink_to(outside_report)
    except OSError:
        pytest.skip("This platform does not permit symlink creation")

    with pytest.raises(HTTPException) as exc:
        api.get_report(linked_report.name)
    assert exc.value.status_code == 404


def test_users_cannot_trace_or_cancel_another_users_run(monkeypatch):
    class Manager:
        def cancel(self):
            raise AssertionError("another user's run must not be cancelled")

    monkeypatch.setattr(api, "_manager", Manager())
    monkeypatch.setattr(api, "_trace_owner", "user:alice")
    monkeypatch.setattr(api, "_active_run_owner", "user:alice")
    acquired = api._run_guard.acquire(blocking=False)
    assert acquired
    try:
        with pytest.raises(HTTPException) as trace_error:
            api.trace(user_id="bob")
        assert trace_error.value.status_code == 403

        with pytest.raises(HTTPException) as cancel_error:
            api.cancel_run(user_id="bob")
        assert cancel_error.value.status_code == 403
    finally:
        api._run_guard.release()


def test_trace_and_cancel_hold_owner_transition_lock(monkeypatch):
    class Tracer:
        def snapshot(self, _since):
            assert not api._RUN_OWNER_LOCK.acquire(blocking=False)
            return {"events": [], "run": {"status": "idle"}}

    class Manager:
        def cancel(self):
            assert not api._RUN_OWNER_LOCK.acquire(blocking=False)

    monkeypatch.setattr(api, "tracer", Tracer())
    monkeypatch.setattr(api, "_manager", Manager())
    monkeypatch.setattr(api, "_trace_owner", "user:alice")
    monkeypatch.setattr(api, "_active_run_owner", "user:alice")

    assert api.trace(user_id="alice")["events"] == []

    acquired = api._run_guard.acquire(blocking=False)
    assert acquired
    try:
        assert api.cancel_run(user_id="alice")["cancelling"] is True
    finally:
        api._run_guard.release()


def test_new_owner_never_inherits_previous_trace(monkeypatch):
    with api._RUN_BUDGET_LOCK:
        api._RUN_STARTS.clear()
    api.tracer.reset()
    api.tracer.run_start("Alice's run")
    monkeypatch.setattr(api, "_trace_owner", "user:alice")

    api._acquire_run_slot("user:bob")
    try:
        snapshot = api.trace(user_id="bob")
        assert snapshot["events"] == []
        assert snapshot["run"]["status"] == "idle"
    finally:
        api._release_run_slot()
