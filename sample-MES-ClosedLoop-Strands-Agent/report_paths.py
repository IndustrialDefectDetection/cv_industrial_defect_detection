"""Safe path handling for generated and served PDF reports."""

from __future__ import annotations

import os
import re
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator


REPORTS_DIR = Path(__file__).resolve().parent / "reports"

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_SAFE_REPORT_FILENAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.pdf$", re.IGNORECASE
)
_UNSAFE_STEM_CHARACTERS = re.compile(r"[^A-Za-z0-9_-]+")


class InvalidReportPath(ValueError):
    """Raised when a report path is invalid or escapes the report directory."""


class UnsupportedReportPlatform(RuntimeError):
    """Raised when the OS cannot provide the required secure file primitives."""


def _require_secure_output_platform() -> None:
    required_dir_fd = (os.open, os.stat, os.unlink, os.link)
    required_follow_symlinks = (os.stat, os.link)
    supported = (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "fchmod")
        and all(function in os.supports_dir_fd for function in required_dir_fd)
        and all(
            function in os.supports_follow_symlinks
            for function in required_follow_symlinks
        )
    )
    if not supported:
        raise UnsupportedReportPlatform(
            "Secure PDF output requires POSIX directory descriptors, "
            "no-follow opens, and owner-only chmod support. This platform "
            "is not supported; no report file was created."
        )


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


@contextmanager
def _open_reports_directory(
    *, create: bool
) -> Iterator[tuple[Path, int]]:
    """Open the report directory without following a final symlink.

    Keeping a directory descriptor open lets report creation use ``openat``
    semantics. A rename of a parent directory cannot redirect the write after
    this check.
    """

    _require_secure_output_platform()
    configured_directory = Path(REPORTS_DIR).expanduser()
    if create:
        configured_directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            configured_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            pass

    try:
        directory_fd = os.open(configured_directory, _directory_open_flags())
    except FileNotFoundError as exc:
        raise InvalidReportPath("The reports directory does not exist.") from exc
    except OSError as exc:
        raise InvalidReportPath(
            "The configured reports path must be a real directory."
        ) from exc

    try:
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise InvalidReportPath(
                "The configured reports path is not a directory."
            )
        os.fchmod(directory_fd, PRIVATE_DIRECTORY_MODE)

        try:
            resolved_directory = configured_directory.resolve(strict=True)
            current_stat = os.stat(configured_directory, follow_symlinks=False)
        except (FileNotFoundError, OSError) as exc:
            raise InvalidReportPath(
                "The reports directory changed during validation."
            ) from exc

        if (
            current_stat.st_dev,
            current_stat.st_ino,
        ) != (
            directory_stat.st_dev,
            directory_stat.st_ino,
        ):
            raise InvalidReportPath(
                "The reports directory changed during validation."
            )

        yield resolved_directory, directory_fd
    finally:
        os.close(directory_fd)


def _ensure_contained(candidate: Path, reports_directory: Path) -> Path:
    try:
        candidate.relative_to(reports_directory)
    except ValueError as exc:
        raise InvalidReportPath(
            "The requested report is outside the reports directory."
        ) from exc
    return candidate


def sanitize_report_stem(
    requested_name: object | None, *, fallback: str = "MES_Report"
) -> str:
    """Turn an untrusted filename hint into a short ASCII basename stem."""

    raw_name = "" if requested_name is None else str(requested_name).strip()
    # Treat both POSIX and Windows separators as path separators, regardless of
    # the operating system running the service.
    leaf_name = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
    if leaf_name.lower().endswith(".pdf"):
        leaf_name = leaf_name[:-4]

    safe_stem = _UNSAFE_STEM_CHARACTERS.sub("_", leaf_name).strip("_-")
    if not safe_stem:
        safe_stem = _UNSAFE_STEM_CHARACTERS.sub("_", fallback).strip("_-")
    if not safe_stem:
        safe_stem = "MES_Report"
    return safe_stem[:64].rstrip("_-") or "MES_Report"


def create_report_path(
    requested_name: object | None = None, *, prefix: str = "MES_Report"
) -> Path:
    """Reserve a unique, server-chosen contained path for a new PDF report.

    ``requested_name`` is only a human-readable hint. The server always adds a
    high-resolution UTC timestamp and random token, preventing the model from
    selecting or overwriting an arbitrary filesystem path. The empty file is
    atomically reserved with owner-only permissions; callers that need to write
    without reopening the path should use :func:`create_report_file`.
    """

    path, file_fd, directory_fd = _allocate_report_file(
        requested_name, prefix=prefix
    )
    os.close(file_fd)
    os.close(directory_fd)
    return path


def _allocate_report_file(
    requested_name: object | None, *, prefix: str
) -> tuple[Path, int, int]:
    """Atomically reserve a new report name and return its file descriptors."""

    safe_stem = sanitize_report_stem(requested_name, fallback=prefix)
    with _open_reports_directory(create=True) as (
        reports_directory,
        directory_fd,
    ):
        for _attempt in range(10):
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            random_token = secrets.token_hex(6)
            filename = f"{safe_stem}_{timestamp}_{random_token}.pdf"
            candidate = reports_directory / filename

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                file_fd = os.open(
                    filename,
                    flags,
                    PRIVATE_FILE_MODE,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise InvalidReportPath(
                    "Could not securely create the report file."
                ) from exc

            try:
                file_stat = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_nlink != 1
                ):
                    raise InvalidReportPath(
                        "The allocated report path is not a regular file."
                    )
                os.fchmod(file_fd, PRIVATE_FILE_MODE)
                retained_directory_fd = os.dup(directory_fd)
            except Exception:
                os.close(file_fd)
                try:
                    os.unlink(filename, dir_fd=directory_fd)
                except OSError:
                    pass
                raise

            # Duplicate the directory descriptor because the context manager
            # closes its copy before the caller starts rendering.
            return candidate, file_fd, retained_directory_fd

    raise InvalidReportPath("Could not allocate a unique report filename.")


def _unlink_if_same_file(path: Path, directory_fd: int, expected: os.stat_result) -> None:
    try:
        current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        return
    try:
        os.unlink(path.name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


@contextmanager
def create_report_file(
    requested_name: object | None = None, *, prefix: str = "MES_Report"
) -> Iterator[tuple[Path, BinaryIO]]:
    """Yield an exclusively-created owner-only file for ReportLab.

    The file is opened with ``O_EXCL`` and ``O_NOFOLLOW`` inside an owner-only
    directory, rendered behind a hidden temporary name, and published only
    after it is complete. Incomplete output is removed without unlinking a
    replacement that does not match the originally-created inode.
    """

    path, file_fd, directory_fd = _allocate_report_file(
        requested_name, prefix=prefix
    )
    original_stat = os.fstat(file_fd)
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    temporary_path = path.with_name(temporary_name)
    try:
        # Move the reserved inode behind a hidden name while ReportLab writes.
        # link/unlink avoids replacing an existing temporary entry.
        os.link(
            path.name,
            temporary_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(path.name, dir_fd=directory_fd)
    except Exception:
        _unlink_if_same_file(path, directory_fd, original_stat)
        _unlink_if_same_file(temporary_path, directory_fd, original_stat)
        os.close(file_fd)
        os.close(directory_fd)
        raise

    try:
        report_file = os.fdopen(file_fd, "wb")
    except Exception:
        os.close(file_fd)
        _unlink_if_same_file(temporary_path, directory_fd, original_stat)
        os.close(directory_fd)
        raise
    completed = False
    try:
        yield path, report_file
        report_file.flush()
        os.fsync(report_file.fileno())
        os.fchmod(report_file.fileno(), PRIVATE_FILE_MODE)
        if os.fstat(report_file.fileno()).st_size == 0:
            raise InvalidReportPath("PDF generation produced an empty file.")
        # Publish the complete inode without overwriting anything that may have
        # appeared at the final random name.
        os.link(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        completed = True
    finally:
        report_file.close()
        if not completed:
            _unlink_if_same_file(path, directory_fd, original_stat)
            _unlink_if_same_file(temporary_path, directory_fd, original_stat)
        os.close(directory_fd)


def resolve_existing_report(filename: object) -> Path:
    """Resolve an existing PDF basename and reject traversal or symlinks."""

    if not isinstance(filename, str):
        raise InvalidReportPath("Report filename must be a string.")

    report_name = filename.strip()
    if (
        not report_name
        or "\x00" in report_name
        or "/" in report_name
        or "\\" in report_name
        or not _SAFE_REPORT_FILENAME.fullmatch(report_name)
    ):
        raise InvalidReportPath("Invalid report filename.")

    with _open_reports_directory(create=False) as (
        reports_directory,
        directory_fd,
    ):
        flags = os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            file_fd = os.open(report_name, flags, dir_fd=directory_fd)
        except FileNotFoundError as exc:
            raise InvalidReportPath("Report not found.") from exc
        except OSError as exc:
            raise InvalidReportPath(
                "Symbolic-link reports are not allowed."
            ) from exc

        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise InvalidReportPath("Report not found.")
            os.fchmod(file_fd, PRIVATE_FILE_MODE)

            unresolved_candidate = reports_directory / report_name
            try:
                resolved_candidate = unresolved_candidate.resolve(strict=True)
                path_stat = os.stat(
                    report_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise InvalidReportPath("Report not found.") from exc

            if (file_stat.st_dev, file_stat.st_ino) != (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                raise InvalidReportPath(
                    "The report changed during validation."
                )
        finally:
            os.close(file_fd)

    _ensure_contained(resolved_candidate, reports_directory)
    if resolved_candidate.suffix.lower() != ".pdf":
        raise InvalidReportPath("Only PDF reports are allowed.")
    return resolved_candidate
