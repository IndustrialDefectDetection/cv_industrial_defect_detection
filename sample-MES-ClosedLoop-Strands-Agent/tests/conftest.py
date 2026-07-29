"""Shared fixtures for the agent tests.

Building a MESAgentManager needs an API key and a reachable Postgres but
costs nothing: __init__ queries the database and builds the agents without
calling Anthropic. So the whole of test_chat_path.py is free to run - only
test_chat_live.py spends credit, and that one is opt-in.
"""
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# The CV pipeline writes detections to Postgres, so that is what the agent
# reads unless a test says otherwise.
os.environ.setdefault("MES_DB_BACKEND", "postgres")


@pytest.fixture
def make_manager(monkeypatch):
    """Build a manager with environment overrides applied first.

    Budgets and limits are read once in __init__, so a test wanting a
    different budget needs its own manager rather than a mutated attribute -
    that way the test exercises the real configuration path.
    """
    from agent_tracer import AgentTracer
    from strands_agent import MESAgentManager

    def factory(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, str(value))
        # Run from the backend directory: DatabaseManager and the SQLite
        # fallback both resolve paths relative to the working directory.
        monkeypatch.chdir(BACKEND_DIR)
        try:
            return MESAgentManager(tracer=AgentTracer())
        except Exception as exc:
            pytest.skip(
                f"agent manager unavailable ({type(exc).__name__}: {exc}). "
                "These tests need ANTHROPIC_API_KEY set and Postgres running."
            )

    return factory


class FakeResult:
    """Stands in for a Strands AgentResult carrying a text reply."""

    def __init__(self, text="supervisor findings"):
        self.message = {"content": [{"text": text}]}


def user(text):
    return {"role": "user", "content": [{"text": text}]}


def assistant(text):
    return {"role": "assistant", "content": [{"text": text}]}


def tool_use(call_id):
    return {"role": "assistant", "content": [{"toolUse": {"toolUseId": call_id}}]}


def tool_result(call_id):
    return {"role": "user", "content": [{"toolResult": {"toolUseId": call_id}}]}
