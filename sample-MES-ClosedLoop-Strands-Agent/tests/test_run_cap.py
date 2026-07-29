"""The hourly cap on camera-triggered investigations.

The one-at-a-time guard bounds concurrency but not spend: a simulator left
looping produces a burst every 30 seconds, and each one is a paid multi-agent
run. These tests pin the cap that bounds the hour.

Pure counter logic - no backend, no model calls.
"""
import importlib
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture
def api(monkeypatch):
    """A freshly imported api module, so the cap is read from the env."""
    def build(max_per_hour=3):
        monkeypatch.setenv("MES_MAX_RUNS_PER_HOUR", str(max_per_hour))
        sys.modules.pop("api", None)
        return importlib.import_module("api")
    yield build
    sys.modules.pop("api", None)


def test_slots_are_granted_up_to_the_cap(api):
    module = api(max_per_hour=3)

    results = [module._claim_investigation_slot() for _ in range(5)]

    assert [allowed for allowed, _ in results] == [True, True, True, False, False]


def test_a_refused_slot_says_when_one_frees(api):
    module = api(max_per_hour=1)
    module._claim_investigation_slot()

    allowed, retry_after = module._claim_investigation_slot()

    assert not allowed
    # Just claimed, so a slot frees in roughly a full hour.
    assert 3500 < retry_after <= 3601


def test_slots_expire_after_an_hour(api):
    """Rolling window, not a fixed bucket: an hour-old run must not count."""
    module = api(max_per_hour=2)
    module._claim_investigation_slot()
    module._claim_investigation_slot()
    assert module._claim_investigation_slot()[0] is False

    # Age both claims past the window.
    module._run_times[:] = [t - 3601 for t in module._run_times]

    assert module._claim_investigation_slot()[0] is True


def test_the_cap_is_at_least_one(api):
    """A cap of 0 would silently disable the camera pipeline entirely."""
    assert api(max_per_hour=0)._MAX_RUNS_PER_HOUR == 1


def test_health_reports_the_cap(api):
    module = api(max_per_hour=7)
    module._claim_investigation_slot()

    health = module.health()

    assert health["max_runs_per_hour"] == 7
    assert health["investigations_last_hour"] == 1
    assert health["run_in_progress"] is False


def test_only_investigate_is_capped(api):
    """A human pressing Run Analysis is self-limiting; refusing them while
    they watch would be worse than the spend."""
    import inspect

    module = api()
    assert "_claim_investigation_slot" in inspect.getsource(module.investigate)
    for handler in (module.run_analysis, module.send_message):
        assert "_claim_investigation_slot" not in inspect.getsource(handler)
