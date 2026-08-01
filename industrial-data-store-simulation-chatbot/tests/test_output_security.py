"""Focused tests for confidential generated operational output."""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime

import pytest

from app_factory.shared import output_security
from app_factory.production_meeting.analysis_cache_manager import (
    AnalysisCacheManager,
)
from app_factory.shared.output_security import (
    PrivateFileHandler,
    UnsupportedOutputPlatform,
    UnsafeOutputPath,
    atomic_write_private_json,
    ensure_private_directory,
    open_private_json,
)


# Windows has no POSIX permission bits, so 0600/0700 cannot be asserted there;
# confidentiality comes from the inherited NTFS ACL instead. Every other
# property in these tests - exclusive creation, no symlink writes, atomic
# publish, no partial output - is checked on both platforms.
POSIX_MODES_ENFORCED = os.name == "posix"


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def assert_private(path, expected_mode) -> None:
    assert path.exists()
    assert not path.is_symlink()
    if POSIX_MODES_ENFORCED:
        assert _mode(path) == expected_mode


def test_private_directory_and_atomic_json_modes(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir(mode=0o755)
    cache = reports / "daily_analysis"

    ensure_private_directory(reports)
    ensure_private_directory(cache)
    output = cache / "daily_analysis_2026-07-31.json"
    atomic_write_private_json(output, {"status": "private"})

    assert_private(reports, 0o700)
    assert_private(cache, 0o700)
    assert_private(output, 0o600)
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "status": "private"
    }
    assert list(cache.glob(".*.tmp")) == []


def test_atomic_json_replaces_symlink_without_touching_target(tmp_path):
    cache = ensure_private_directory(tmp_path / "cache")
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    output = cache / "daily_analysis_2026-07-31.json"
    try:
        output.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links are not supported in this environment")

    atomic_write_private_json(output, {"safe": True})

    assert not output.is_symlink()
    assert json.loads(output.read_text(encoding="utf-8")) == {"safe": True}
    assert outside.read_text(encoding="utf-8") == '{"secret": true}'
    assert_private(output, 0o600)


def test_failed_json_serialization_leaves_no_partial_output(tmp_path):
    cache = ensure_private_directory(tmp_path / "cache")
    output = cache / "daily_analysis_2026-07-31.json"

    with pytest.raises(TypeError):
        atomic_write_private_json(output, {"invalid": object()})

    assert not output.exists()
    assert list(cache.glob(".*.tmp")) == []


def test_private_json_reader_rejects_links_and_repairs_legacy_mode(tmp_path):
    cache = ensure_private_directory(tmp_path / "cache")
    output = cache / "daily_analysis_2026-07-31.json"
    output.write_text('{"safe": true}', encoding="utf-8")
    output.chmod(0o644)

    with open_private_json(output) as source:
        assert json.load(source) == {"safe": True}
    assert_private(output, 0o600)

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    linked = cache / "daily_analysis_2026-07-30.json"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links are not supported in this environment")

    with pytest.raises(UnsafeOutputPath):
        open_private_json(linked)


def test_output_directory_symlink_is_rejected(tmp_path):
    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    linked_cache = tmp_path / "cache"
    try:
        linked_cache.symlink_to(real_cache, target_is_directory=True)
    except OSError:
        pytest.skip("Symbolic links are not supported in this environment")

    with pytest.raises(UnsafeOutputPath):
        ensure_private_directory(linked_cache)


def test_unsupported_platform_fails_closed_before_creating_output(
    tmp_path, monkeypatch,
):
    """Windows used to land here; it now has its own implementation.

    The guarantee being pinned is unchanged: a platform with neither the POSIX
    primitives nor the Windows ones writes nothing at all rather than falling
    back to an unprotected write.
    """
    cache = tmp_path / "cache"
    monkeypatch.setattr(output_security.os, "name", "java")

    with pytest.raises(UnsupportedOutputPlatform, match="not supported"):
        ensure_private_directory(cache)

    assert not cache.exists()


def test_private_log_handler_uses_owner_only_append_file(tmp_path):
    log_path = tmp_path / "logs" / "daily_analysis.log"
    handler = PrivateFileHandler(log_path)
    logger = logging.getLogger(f"test-private-log-{id(handler)}")
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("operational detail")
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert_private(log_path.parent, 0o700)
    assert_private(log_path, 0o600)
    assert "operational detail" in log_path.read_text(encoding="utf-8")


def test_private_log_handler_rejects_symlink(tmp_path):
    logs = ensure_private_directory(tmp_path / "logs")
    outside = tmp_path / "outside.log"
    outside.write_text("outside", encoding="utf-8")
    linked_log = logs / "daily_analysis.log"
    try:
        linked_log.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links are not supported in this environment")

    with pytest.raises(UnsafeOutputPath):
        PrivateFileHandler(linked_log)
    assert outside.read_text(encoding="utf-8") == "outside"


def test_scheduler_cache_is_atomic_private_and_loadable(
    tmp_path, monkeypatch
):
    from app_factory.production_meeting import daily_analysis_scheduler as module

    monkeypatch.setattr(module, "DatabaseManager", lambda: object())
    monkeypatch.setattr(
        module, "ProductionMeetingAgentManager", lambda _config: object()
    )
    cache = tmp_path / "reports" / "daily_analysis"
    scheduler = module.DailyAnalysisScheduler(
        cache_dir=str(cache), generate_data=False
    )
    generated_at = datetime(2026, 7, 31, 12, 0, 0)
    payload = {
        "generated_at": generated_at.isoformat(),
        "analysis_date": "2026-07-31",
        "analyses": {"quality": {"analysis": "private"}},
    }

    scheduler.save_analysis_cache(payload, generated_at)
    output = scheduler.get_cache_filename(generated_at)
    manager = AnalysisCacheManager(str(cache))

    assert manager.load_cached_analysis(generated_at) == payload
    assert_private(cache.parent, 0o700)
    assert_private(cache, 0o700)
    assert_private(output, 0o600)
