"""Safe path handling for generated and served PDF reports.

Two implementations of one contract, chosen by platform.

The properties every platform must provide:

* the model never chooses the final filename - the server appends a UTC
  timestamp and a random token, so a name hint cannot select or overwrite an
  arbitrary path;
* reports are created exclusively (``O_EXCL``), never opened over an existing
  file, and never published over a name that appeared in the meantime;
* the finished path is contained inside the reports directory, and neither the
  directory nor the file may be a symlink or reparse point;
* incomplete output is removed rather than left behind as a truncated PDF.

What differs is *how strongly the filesystem enforces it*. POSIX keeps a
directory descriptor open and does every step relative to it, so a rename of a
parent directory cannot redirect the write after validation, and ``fchmod``
pins owner-only permissions on the descriptor itself. Windows has no
``openat``, no ``O_NOFOLLOW`` and no POSIX permission bits, so the Windows path
validates by path instead of by descriptor and relies on the directory's
inherited NTFS ACL for confidentiality rather than setting 0600 explicitly.

That is a genuine, deliberate difference: on Windows a sufficiently privileged
local attacker who can win a race against directory validation is not defeated
by this module the way they are on POSIX. It is not a reason to refuse to run
- these reports are generated on a developer's machine and served over
localhost - but production deployment belongs on Linux, where the stronger
path is the one that executes.

A platform that is neither POSIX-with-``openat`` nor Windows still fails
closed: no file is created at all.
"""

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
_ALLOCATION_ATTEMPTS = 10


class InvalidReportPath(ValueError):
    """Raised when a report path is invalid or escapes the report directory."""


class UnsupportedReportPlatform(RuntimeError):
    """Raised when the OS cannot provide the required secure file primitives."""


def _posix_primitives_available() -> bool:
    """True when this OS can do descriptor-relative, no-follow, owner-only IO."""

    required_dir_fd = (os.open, os.stat, os.unlink, os.link)
    required_follow_symlinks = (os.stat, os.link)
    return (
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


def _windows_primitives_available() -> bool:
    return os.name == "nt"


def _require_secure_output_platform() -> None:
    if _posix_primitives_available() or _windows_primitives_available():
        return
    raise UnsupportedReportPlatform(
        "Secure PDF output requires either POSIX directory descriptors with "
        "no-follow opens and owner-only chmod support, or Windows. This "
        "platform is not supported; no report file was created."
    )


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


def _generated_report_name(safe_stem: str) -> str:
    """A server-chosen name a filename hint cannot collide with on purpose."""

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{safe_stem}_{timestamp}_{secrets.token_hex(6)}.pdf"


def _validate_report_filename(filename: object) -> str:
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
    return report_name


# --------------------------------------------------------------------- POSIX


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


def _allocate_report_file(
    requested_name: object | None, *, prefix: str
) -> tuple[Path, int, int]:
    """Atomically reserve a new report name and return its file descriptors."""

    safe_stem = sanitize_report_stem(requested_name, fallback=prefix)
    with _open_reports_directory(create=True) as (
        reports_directory,
        directory_fd,
    ):
        for _attempt in range(_ALLOCATION_ATTEMPTS):
            filename = _generated_report_name(safe_stem)
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


def _posix_create_report_path(
    requested_name: object | None, *, prefix: str
) -> Path:
    path, file_fd, directory_fd = _allocate_report_file(
        requested_name, prefix=prefix
    )
    os.close(file_fd)
    os.close(directory_fd)
    return path


@contextmanager
def _posix_create_report_file(
    requested_name: object | None, *, prefix: str
) -> Iterator[tuple[Path, BinaryIO]]:
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


def _posix_resolve_existing_report(report_name: str) -> Path:
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

    return _ensure_contained(resolved_candidate, reports_directory)


# ------------------------------------------------------------------- Windows


def _is_reparse_point(entry: os.stat_result) -> bool:
    """True for symlinks and junctions, which must never be written through."""

    attributes = getattr(entry, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _windows_reports_directory(*, create: bool) -> Path:
    """Validate and resolve the reports directory by path.

    Windows has no ``O_DIRECTORY``/``openat``, so this cannot hold the
    directory open across the write the way the POSIX path does. It still
    refuses to write through a symlink or junction, which is the escape that
    matters in practice.
    """

    configured_directory = Path(REPORTS_DIR).expanduser()
    if create:
        configured_directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            configured_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            pass

    try:
        directory_entry = os.lstat(configured_directory)
    except FileNotFoundError as exc:
        raise InvalidReportPath("The reports directory does not exist.") from exc
    except OSError as exc:
        raise InvalidReportPath(
            "The configured reports path must be a real directory."
        ) from exc

    if stat.S_ISLNK(directory_entry.st_mode) or _is_reparse_point(
        directory_entry
    ):
        raise InvalidReportPath(
            "The configured reports path must be a real directory."
        )
    if not stat.S_ISDIR(directory_entry.st_mode):
        raise InvalidReportPath(
            "The configured reports path is not a directory."
        )
    return configured_directory.resolve(strict=True)


def _windows_open_exclusive(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    return os.open(path, flags, PRIVATE_FILE_MODE)


def _windows_quiet_unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _windows_create_report_path(
    requested_name: object | None, *, prefix: str
) -> Path:
    safe_stem = sanitize_report_stem(requested_name, fallback=prefix)
    reports_directory = _windows_reports_directory(create=True)

    for _attempt in range(_ALLOCATION_ATTEMPTS):
        candidate = reports_directory / _generated_report_name(safe_stem)
        try:
            file_fd = _windows_open_exclusive(candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            raise InvalidReportPath(
                "Could not securely create the report file."
            ) from exc
        os.close(file_fd)
        return candidate

    raise InvalidReportPath("Could not allocate a unique report filename.")


@contextmanager
def _windows_create_report_file(
    requested_name: object | None, *, prefix: str
) -> Iterator[tuple[Path, BinaryIO]]:
    """Render into a hidden temporary file, then publish it by rename.

    The final name is reserved by *not* creating it: ``os.rename`` on Windows
    fails when the destination exists, so publishing cannot overwrite a file
    that appeared while ReportLab was writing. That is the same guarantee the
    POSIX path gets from ``link``, without needing to unlink a file that is
    still open - which Windows does not allow.
    """

    safe_stem = sanitize_report_stem(requested_name, fallback=prefix)
    reports_directory = _windows_reports_directory(create=True)

    path: Path | None = None
    file_fd: int | None = None
    for _attempt in range(_ALLOCATION_ATTEMPTS):
        candidate = reports_directory / _generated_report_name(safe_stem)
        temporary_candidate = candidate.with_name(
            f".{candidate.name}.{secrets.token_hex(8)}.tmp"
        )
        if candidate.exists():
            continue
        try:
            file_fd = _windows_open_exclusive(temporary_candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            raise InvalidReportPath(
                "Could not securely create the report file."
            ) from exc
        path = candidate
        temporary_path = temporary_candidate
        break

    if path is None or file_fd is None:
        raise InvalidReportPath("Could not allocate a unique report filename.")

    try:
        report_file = os.fdopen(file_fd, "wb")
    except Exception:
        os.close(file_fd)
        _windows_quiet_unlink(temporary_path)
        raise

    completed = False
    try:
        yield path, report_file
        report_file.flush()
        os.fsync(report_file.fileno())
        if os.fstat(report_file.fileno()).st_size == 0:
            raise InvalidReportPath("PDF generation produced an empty file.")
        # The handle must be closed before the rename: Windows will not move a
        # file that is still open.
        report_file.close()
        os.rename(temporary_path, path)
        completed = True
    finally:
        if not report_file.closed:
            report_file.close()
        if not completed:
            # Only the temporary file is ours to remove. Anything sitting at
            # the final name was put there by something else - deleting it
            # would turn a refused publish into data loss.
            _windows_quiet_unlink(temporary_path)


def _windows_resolve_existing_report(report_name: str) -> Path:
    reports_directory = _windows_reports_directory(create=False)
    candidate = reports_directory / report_name

    try:
        entry = os.lstat(candidate)
    except FileNotFoundError as exc:
        raise InvalidReportPath("Report not found.") from exc
    except OSError as exc:
        raise InvalidReportPath("Report not found.") from exc

    if stat.S_ISLNK(entry.st_mode) or _is_reparse_point(entry):
        raise InvalidReportPath("Symbolic-link reports are not allowed.")
    if not stat.S_ISREG(entry.st_mode):
        raise InvalidReportPath("Report not found.")

    try:
        resolved_candidate = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise InvalidReportPath("Report not found.") from exc

    return _ensure_contained(resolved_candidate, reports_directory)


# -------------------------------------------------------------- public entry


def create_report_path(
    requested_name: object | None = None, *, prefix: str = "MES_Report"
) -> Path:
    """Reserve a unique, server-chosen contained path for a new PDF report.

    ``requested_name`` is only a human-readable hint. The server always adds a
    high-resolution UTC timestamp and random token, preventing the model from
    selecting or overwriting an arbitrary filesystem path. The empty file is
    atomically reserved; callers that need to write without reopening the path
    should use :func:`create_report_file`.
    """

    _require_secure_output_platform()
    if _posix_primitives_available():
        return _posix_create_report_path(requested_name, prefix=prefix)
    return _windows_create_report_path(requested_name, prefix=prefix)


@contextmanager
def create_report_file(
    requested_name: object | None = None, *, prefix: str = "MES_Report"
) -> Iterator[tuple[Path, BinaryIO]]:
    """Yield an exclusively-created file for ReportLab.

    The report is rendered behind a hidden temporary name and published only
    once it is complete, so a reader never sees a truncated PDF and an
    interrupted render leaves nothing behind. Publishing never overwrites a
    file that appeared at the final name in the meantime.
    """

    _require_secure_output_platform()
    if _posix_primitives_available():
        implementation = _posix_create_report_file
    else:
        implementation = _windows_create_report_file
    with implementation(requested_name, prefix=prefix) as opened_report:
        yield opened_report


def resolve_existing_report(filename: object) -> Path:
    """Resolve an existing PDF basename and reject traversal or symlinks."""

    report_name = _validate_report_filename(filename)
    _require_secure_output_platform()
    if _posix_primitives_available():
        resolved_candidate = _posix_resolve_existing_report(report_name)
    else:
        resolved_candidate = _windows_resolve_existing_report(report_name)

    if resolved_candidate.suffix.lower() != ".pdf":
        raise InvalidReportPath("Only PDF reports are allowed.")
    return resolved_candidate
