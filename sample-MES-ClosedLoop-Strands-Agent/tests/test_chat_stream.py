"""Regression coverage for the long-running chat heartbeat transport."""

import asyncio
import json
import queue
import threading

import api
import pytest
from strands_agent import RunCancelled


class FakeResult:
    def __init__(self, text="finished", stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.message = {"content": [{"text": text}]}


class FakeManager:
    def __init__(self, result=None, error=None):
        self.result = result or FakeResult()
        self.error = error
        self.cancelled = False
        self.reset_calls = 0
        self.chat_calls = []
        self.prepare_calls = 0

    def prepare_chat_request(self):
        self.cancelled = False
        self.prepare_calls += 1

    def run_chat(self, query, history):
        self.chat_calls.append((query, history))
        if self.error:
            raise self.error
        return self.result

    def cancel(self):
        self.cancelled = True

    def reset_chat_history(self):
        self.reset_calls += 1


def chat_request(
    user_input="run it",
    conversation_id="550e8400-e29b-41d4-a716-446655440000",
    history=None,
):
    return api.ChatRequest(
        conversation_id=conversation_id,
        user_input=user_input,
        history=[] if history is None else history,
    )


async def response_lines(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return [
        json.loads(line)
        for line in "".join(chunks).splitlines()
        if line.strip()
    ]


def test_chat_stream_starts_immediately_and_finishes_after_unlock(monkeypatch):
    manager = FakeManager(FakeResult("the report"))
    monkeypatch.setattr(api, "_manager", manager)

    response = api.send_message(chat_request(), user_id="user-1")
    events = asyncio.run(response_lines(response))

    assert response.media_type == "application/x-ndjson"
    assert events == [
        {"type": "started"},
        {"type": "result", "data": {"analysis": "the report"}},
    ]
    assert manager.prepare_calls == 1
    assert not api._run_guard.locked()


def test_chat_request_preparation_runs_inside_owner_transition(monkeypatch):
    prepared = []

    def prepare():
        assert api._run_guard.locked()
        assert api._active_run_owner == "user:alice"
        assert not api._RUN_OWNER_LOCK.acquire(blocking=False)
        prepared.append(True)

    with api._RUN_BUDGET_LOCK:
        api._RUN_STARTS.clear()
    api._acquire_run_slot("user:alice", on_acquired=prepare)
    try:
        assert prepared == [True]
    finally:
        api._release_run_slot()


def test_failed_chat_request_preparation_releases_run_slot():
    def fail_preparation():
        raise RuntimeError("preparation failed")

    with api._RUN_BUDGET_LOCK:
        api._RUN_STARTS.clear()
    try:
        with pytest.raises(RuntimeError, match="preparation failed"):
            api._acquire_run_slot(
                "user:alice",
                on_acquired=fail_preparation,
            )

        assert api._active_run_owner is None
        assert api._trace_owner is None
        assert not api._run_guard.locked()
    finally:
        if api._run_guard.locked():
            api._release_run_slot()


def test_cancelled_run_is_a_terminal_result(monkeypatch):
    manager = FakeManager(error=RunCancelled("stop"))
    monkeypatch.setattr(api, "_manager", manager)

    response = api.send_message(chat_request(), user_id="user-1")
    events = asyncio.run(response_lines(response))

    assert events[-1] == {
        "type": "result",
        "data": {"status": "cancelled"},
    }
    assert not api._run_guard.locked()


def test_failed_run_is_streamed_after_unlock(monkeypatch):
    manager = FakeManager(error=ValueError("bad result"))
    monkeypatch.setattr(api, "_manager", manager)

    response = api.send_message(chat_request(), user_id="user-1")
    events = asyncio.run(response_lines(response))

    assert events[-1] == {
        "type": "error",
        "error": "The analysis failed",
    }
    assert "bad result" not in json.dumps(api.tracer.snapshot(0))
    assert response.headers["cache-control"] == "private, no-store, no-transform"
    assert not api._run_guard.locked()


def test_each_conversation_supplies_its_own_saved_history(monkeypatch):
    manager = FakeManager(FakeResult("private-safe"))
    monkeypatch.setattr(api, "_manager", manager)

    first_history = [
        {"role": "user", "content": "maintenance correlation"},
        {"role": "assistant", "content": "Which machines?"},
    ]
    first_response = api.send_message(
        chat_request(
            user_input="choose random machines",
            history=first_history,
        ),
        user_id="same-user",
    )
    asyncio.run(response_lines(first_response))

    second_response = api.send_message(
        chat_request(
            user_input="start fresh",
            conversation_id="6ba7b810-9dad-11d1-80b4-00c04fd430c8",
            history=[],
        ),
        user_id="same-user",
    )
    asyncio.run(response_lines(second_response))

    assert manager.chat_calls == [
        ("choose random machines", first_history),
        ("start fresh", []),
    ]


def test_stream_sends_heartbeats_until_the_terminal_event():
    terminal_event = {
        "type": "result",
        "data": {"analysis": "finished"},
    }

    class DelayedEvents:
        calls = 0

        def get(self, timeout):
            assert timeout == api._CHAT_HEARTBEAT_SECONDS
            self.calls += 1
            if self.calls == 1:
                raise queue.Empty
            return terminal_event

    manager = FakeManager()
    worker = threading.Thread(target=lambda: None)
    events = [
        json.loads(line)
        for line in api._iter_chat_events(
            manager,
            worker,
            DelayedEvents(),
            "user:test",
        )
    ]

    assert events == [
        {"type": "started"},
        {"type": "heartbeat"},
        terminal_event,
    ]


def test_disconnect_requests_backend_cancellation():
    stopped = threading.Event()

    class BlockingManager(FakeManager):
        def cancel(self):
            super().cancel()
            stopped.set()

    manager = BlockingManager()
    worker = threading.Thread(target=stopped.wait, daemon=True)
    worker.start()
    events = queue.Queue()
    with api._RUN_BUDGET_LOCK:
        api._RUN_STARTS.clear()
    api._acquire_run_slot("user:test")
    stream = api._iter_chat_events(
        manager,
        worker,
        events,
        "user:test",
    )

    try:
        assert json.loads(next(stream)) == {"type": "started"}
        stream.close()
        worker.join(timeout=1)

        assert manager.cancelled
        assert not worker.is_alive()
    finally:
        if api._run_guard.locked():
            api._release_run_slot()


def test_cancel_endpoint_finishes_the_original_stream_and_releases_guard(
    monkeypatch,
):
    class BlockingManager(FakeManager):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.stop_requested = threading.Event()

        def run_chat(self, query, history):
            self.started.set()
            if not self.stop_requested.wait(timeout=1):
                raise AssertionError("cancel did not reach the chat worker")
            raise RunCancelled("cancelled")

        def cancel(self):
            super().cancel()
            self.stop_requested.set()

    manager = BlockingManager()
    monkeypatch.setattr(api, "_manager", manager)
    with api._RUN_BUDGET_LOCK:
        api._RUN_STARTS.clear()

    response = api.send_message(chat_request(), user_id="same-user")
    events = []

    def consume_stream():
        events.extend(asyncio.run(response_lines(response)))

    stream_thread = threading.Thread(target=consume_stream, daemon=True)
    stream_thread.start()
    try:
        assert manager.started.wait(timeout=1)
        cancellation = api.cancel_run(user_id="same-user")
        stream_thread.join(timeout=1)

        assert cancellation == {
            "cancelling": True,
            "detail": "Cancelling the run",
        }
        assert not stream_thread.is_alive()
        assert events == [
            {"type": "started"},
            {"type": "result", "data": {"status": "cancelled"}},
        ]
        assert not api._run_guard.locked()
    finally:
        manager.stop_requested.set()
        stream_thread.join(timeout=1)
        if api._run_guard.locked():
            api._release_run_slot()


def test_old_disconnect_cannot_cancel_a_new_owners_run():
    manager = FakeManager()

    class AliveWorker:
        @staticmethod
        def is_alive():
            return True

    with api._RUN_BUDGET_LOCK:
        api._RUN_STARTS.clear()
    api._acquire_run_slot("user:bob")
    stream = api._iter_chat_events(
        manager,
        AliveWorker(),
        queue.Queue(),
        "user:alice",
    )
    try:
        assert json.loads(next(stream)) == {"type": "started"}
        stream.close()
        assert manager.cancelled is False
    finally:
        api._release_run_slot()
