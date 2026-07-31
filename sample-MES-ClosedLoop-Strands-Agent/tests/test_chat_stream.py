"""Regression coverage for the long-running chat heartbeat transport."""

import asyncio
import json
import queue
import threading

import api
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

    def run_chat(self, query):
        if self.error:
            raise self.error
        return self.result

    def cancel(self):
        self.cancelled = True

    def reset_chat_history(self):
        self.reset_calls += 1


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

    response = api.send_message(api.ChatRequest(user_input="run it"), user_id="user-1")
    events = asyncio.run(response_lines(response))

    assert response.media_type == "application/x-ndjson"
    assert events == [
        {"type": "started"},
        {"type": "result", "data": {"analysis": "the report"}},
    ]
    assert not api._run_guard.locked()


def test_cancelled_run_is_a_terminal_result(monkeypatch):
    manager = FakeManager(error=RunCancelled("stop"))
    monkeypatch.setattr(api, "_manager", manager)

    response = api.send_message(api.ChatRequest(user_input="run it"), user_id="user-1")
    events = asyncio.run(response_lines(response))

    assert events[-1] == {
        "type": "result",
        "data": {"status": "cancelled"},
    }
    assert not api._run_guard.locked()


def test_failed_run_is_streamed_after_unlock(monkeypatch):
    manager = FakeManager(error=ValueError("bad result"))
    monkeypatch.setattr(api, "_manager", manager)

    response = api.send_message(api.ChatRequest(user_input="run it"), user_id="user-1")
    events = asyncio.run(response_lines(response))

    assert events[-1] == {
        "type": "error",
        "error": "The analysis failed",
    }
    assert "bad result" not in json.dumps(api.tracer.snapshot(0))
    assert response.headers["cache-control"] == "private, no-store, no-transform"
    assert not api._run_guard.locked()


def test_a_different_user_gets_a_fresh_conversation(monkeypatch):
    manager = FakeManager(FakeResult("private-safe"))
    monkeypatch.setattr(api, "_manager", manager)
    monkeypatch.setattr(api, "_conversation_owner", "user:alice")

    response = api.send_message(
        api.ChatRequest(user_input="my first question"),
        user_id="bob",
    )
    asyncio.run(response_lines(response))

    assert manager.reset_calls == 1
    assert api._conversation_owner == "user:bob"


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
