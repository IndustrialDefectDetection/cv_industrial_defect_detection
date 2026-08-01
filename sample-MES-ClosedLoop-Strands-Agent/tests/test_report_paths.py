"""Focused tests for generated and user-selected PDF report paths."""

from __future__ import annotations

import stat

import pytest

import report_paths
from report_paths import (
    InvalidReportPath,
    UnsupportedReportPlatform,
    create_report_file,
    create_report_path,
    resolve_existing_report,
    sanitize_report_stem,
)


@pytest.fixture
def reports_directory(tmp_path, monkeypatch):
    reports_path = tmp_path / "reports"
    monkeypatch.setattr(report_paths, "REPORTS_DIR", reports_path)
    return reports_path


@pytest.mark.parametrize(
    ("untrusted_name", "expected_stem"),
    [
        ("../../outside/evil.pdf", "evil"),
        (r"..\..\outside\windows.pdf", "windows"),
        ("/tmp/absolute.pdf", "absolute"),
        ("quarterly report (final).PDF", "quarterly_report_final"),
    ],
)
def test_report_name_hints_are_sanitized_to_basenames(
    untrusted_name, expected_stem
):
    assert sanitize_report_stem(untrusted_name) == expected_stem


def test_new_report_path_is_server_generated_and_contained(reports_directory):
    chosen_path = create_report_path(
        "../../outside/evil.pdf", prefix="MES_Analysis_Report"
    )
    second_path = create_report_path(
        "../../outside/evil.pdf", prefix="MES_Analysis_Report"
    )

    resolved_reports_directory = reports_directory.resolve()
    assert chosen_path.parent == resolved_reports_directory
    assert chosen_path.suffix == ".pdf"
    assert chosen_path.name.startswith("evil_")
    assert chosen_path != second_path
    assert ".." not in chosen_path.name
    assert chosen_path.is_file()
    assert second_path.is_file()
    assert stat.S_IMODE(chosen_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(second_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(reports_directory.stat().st_mode) == 0o700


def test_report_file_is_exclusive_private_and_removed_when_incomplete(
    reports_directory,
):
    with create_report_file("private report.pdf") as (report, output):
        output.write(b"%PDF-private")
        assert not report.exists()

    assert report.read_bytes() == b"%PDF-private"
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert list(reports_directory.glob(".*.tmp")) == []

    with pytest.raises(RuntimeError, match="interrupted"):
        with create_report_file("interrupted.pdf") as (
            incomplete_report,
            output,
        ):
            output.write(b"partial")
            raise RuntimeError("interrupted")

    assert not incomplete_report.exists()
    assert list(reports_directory.glob(".*.tmp")) == []


def test_completed_report_does_not_overwrite_a_replaced_final_name(
    reports_directory,
):
    with pytest.raises(FileExistsError):
        with create_report_file("race.pdf") as (report, output):
            output.write(b"%PDF-complete")
            report.write_bytes(b"replacement")

    assert report.read_bytes() == b"replacement"
    assert list(reports_directory.glob(".*.tmp")) == []


def test_model_cannot_choose_or_overwrite_an_existing_report(reports_directory):
    reports_directory.mkdir(parents=True)
    existing_report = reports_directory / "existing.pdf"
    existing_report.write_bytes(b"original")

    generated_path = create_report_path("existing.pdf")

    assert generated_path != existing_report
    assert existing_report.read_bytes() == b"original"
    assert generated_path.is_file()
    assert generated_path.read_bytes() == b""
    assert stat.S_IMODE(generated_path.stat().st_mode) == 0o600


def test_final_report_writer_uses_server_generated_contained_path(
    reports_directory,
):
    from strands_agent import render_markdown_report_pdf

    generated_report = render_markdown_report_pdf(
        "# Contained report", "../../outside/model-chosen.pdf"
    )

    assert generated_report.is_file()
    assert generated_report.parent == reports_directory.resolve()
    assert generated_report.name.startswith("model-chosen_")
    assert stat.S_IMODE(reports_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(generated_report.stat().st_mode) == 0o600


def test_existing_report_resolver_accepts_safe_pdf(reports_directory):
    reports_directory.mkdir(parents=True)
    report = reports_directory / "safe-report_2026.pdf"
    report.write_bytes(b"%PDF-safe")

    resolved = resolve_existing_report(report.name)

    assert resolved == report.resolve()
    assert stat.S_IMODE(report.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "filename",
    [
        "../secret.pdf",
        "..%2Fsecret.pdf",
        "/tmp/secret.pdf",
        r"..\secret.pdf",
        "not-a-pdf.txt",
        ".hidden.pdf",
        "",
    ],
)
def test_existing_report_resolver_rejects_traversal_and_invalid_names(
    reports_directory, filename
):
    reports_directory.mkdir(parents=True)

    with pytest.raises(InvalidReportPath):
        resolve_existing_report(filename)


def test_existing_report_resolver_rejects_symlink_escape(
    reports_directory, tmp_path
):
    reports_directory.mkdir(parents=True)
    outside_report = tmp_path / "outside.pdf"
    outside_report.write_bytes(b"%PDF-outside")
    linked_report = reports_directory / "linked.pdf"

    try:
        linked_report.symlink_to(outside_report)
    except OSError:
        pytest.skip("Symbolic links are not supported in this environment")

    with pytest.raises(InvalidReportPath):
        resolve_existing_report(linked_report.name)


def test_report_directory_symlink_is_rejected(tmp_path, monkeypatch):
    real_reports = tmp_path / "real-reports"
    real_reports.mkdir()
    linked_reports = tmp_path / "reports"
    try:
        linked_reports.symlink_to(real_reports, target_is_directory=True)
    except OSError:
        pytest.skip("Symbolic links are not supported in this environment")

    monkeypatch.setattr(report_paths, "REPORTS_DIR", linked_reports)

    with pytest.raises(InvalidReportPath):
        with create_report_file("unsafe.pdf") as _report:
            pass


def test_unsupported_platform_fails_closed_before_creating_output(
    tmp_path, monkeypatch,
):
    reports = tmp_path / "reports"
    monkeypatch.setattr(report_paths, "REPORTS_DIR", reports)
    monkeypatch.setattr(report_paths.os, "name", "nt")

    with pytest.raises(UnsupportedReportPlatform, match="not supported"):
        create_report_path("unsupported.pdf")

    assert not reports.exists()
