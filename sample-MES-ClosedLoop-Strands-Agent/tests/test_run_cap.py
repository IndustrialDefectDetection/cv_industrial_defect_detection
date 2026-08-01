"""The hourly cap on cost-bearing agent runs.

The one-at-a-time guard bounds concurrency but not spend: a simulator left
looping produces a burst every 30 seconds, and each one is a paid multi-agent
run. `_reserve_run_budget` bounds the hour instead, and every run slot
(/investigate, /analysis, /chat/) goes through it.

test_api_security.py pins the 429 and its Retry-After header. These tests pin
the two properties it does not: the window rolls, and the env value is clamped.

Pure counter logic - no backend, no model calls.
"""
import importlib
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import api  # noqa: E402


@pytest.fixture(autouse=True)
def empty_budget():
    """Each test starts and ends with no runs recorded in the window."""
    with api._RUN_BUDGET_LOCK:
        api._RUN_STARTS.clear()
    yield
    with api._RUN_BUDGET_LOCK:
        api._RUN_STARTS.clear()


def test_starts_expire_after_an_hour(monkeypatch):
    """Rolling window, not a fixed bucket: an hour-old run must not count."""
    monkeypatch.setattr(api, "_MAX_RUNS_PER_HOUR", 2)
    api._reserve_run_budget()
    api._reserve_run_budget()
    with pytest.raises(HTTPException):
        api._reserve_run_budget()

    # Age both starts past the window.
    with api._RUN_BUDGET_LOCK:
        aged = [t - api._RUN_BUDGET_WINDOW_SECONDS - 1 for t in api._RUN_STARTS]
        api._RUN_STARTS.clear()
        api._RUN_STARTS.extend(aged)

    api._reserve_run_budget()  # must not raise


@pytest.mark.parametrize(
    "configured, expected",
    [
        ("0", 1),  # a cap of 0 would silently disable the pipeline entirely
        ("100000", 100),  # nor can the env lift the ceiling arbitrarily
        ("not-a-number", 10),  # a typo falls back to the default, not a crash
    ],
)
def test_the_env_cap_is_clamped(monkeypatch, configured, expected):
    monkeypatch.setenv("MES_MAX_RUNS_PER_HOUR", configured)

    assert api._bounded_int_env(
        "MES_MAX_RUNS_PER_HOUR", default=10, minimum=1, maximum=100
    ) == expected


def test_every_run_slot_goes_through_the_budget():
    """Not just /investigate: a human clicking Run Analysis costs the same."""
    import inspect

    assert "_reserve_run_budget" in inspect.getsource(api._acquire_run_slot)
    for handler in (api.investigate, api.run_analysis, api.send_message):
        assert "_acquire_run_slot" in inspect.getsource(handler)
