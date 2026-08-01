"""Owner-only, race-resistant helpers for generated operational output.

Cached analyses and scheduler logs carry machine, order and operator detail,
so they are written privately: created exclusively, never through a symlink,
never left half-written, and published by an atomic rename.

There are two implementations. POSIX does every step relative to an open
directory descriptor, so a swapped parent directory cannot redirect the write
after validation, and ``fchmod`` pins 0600/0700 on the descriptor itself.
Windows has no ``openat``, no ``O_NOFOLLOW`` and no POSIX permission bits, so
it validates by path and inherits confidentiality from the NTFS ACL of the
containing directory. The Windows path is weaker against a local attacker
racing directory validation; it exists so a developer machine can run the
dashboard and the scheduler, while production stays on Linux.

A platform that is neither still fails closed - no output file is created.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class UnsafeOutputPath(ValueError):
    """Raised when an output path is missing, linked, or changes during use."""


class UnsupportedOutputPlatform(RuntimeError):
    """Raised when secure owner-only output cannot be guaranteed."""


def _posix_primitives_available() -> bool:
    required_dir_fd = (os.open, os.stat, os.unlink, os.rename)
    return (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "fchmod")
        and all(function in os.supports_dir_fd for function in required_dir_fd)
        and os.stat in os.supports_follow_symlinks
    )


def _windows_primitives_available() -> bool:
    return os.name == "nt"


def _require_secure_output_platform() -> None:
    if _posix_primitives_available() or _windows_primitives_available():
        return
    raise UnsupportedOutputPlatform(
        "Secure generated output requires either POSIX directory descriptors "
        "with no-follow opens and owner-only chmod support, or Windows. This "
        "platform is not supported; no output file was created."
    )


def _is_reparse_point(entry: os.stat_result) -> bool:
    """True for Windows symlinks and junctions."""

    attributes = getattr(entry, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _windows_private_directory(
    directory: str | os.PathLike[str], *, create: bool
) -> Path:
    configured = Path(directory).expanduser()
    if create:
        configured.parent.mkdir(parents=True, exist_ok=True)
        try:
            configured.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            pass

    try:
        entry = os.lstat(configured)
    except FileNotFoundError as exc:
        raise UnsafeOutputPath("Output directory does not exist.") from exc
    except OSError as exc:
        raise UnsafeOutputPath(
            "Output directory must not be a symbolic link."
        ) from exc

    if stat.S_ISLNK(entry.st_mode) or _is_reparse_point(entry):
        raise UnsafeOutputPath("Output directory must not be a symbolic link.")
    if not stat.S_ISDIR(entry.st_mode):
        raise UnsafeOutputPath("Output path is not a directory.")
    return configured.resolve(strict=True)


def _windows_reject_link(path: Path, message: str) -> os.stat_result | None:
    """Return the entry for ``path``, refusing symlinks and junctions."""

    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UnsafeOutputPath(message) from exc

    if stat.S_ISLNK(entry.st_mode) or _is_reparse_point(entry):
        raise UnsafeOutputPath(message)
    return entry


def _directory_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


@contextmanager
def _posix_open_private_directory(
    directory: str | os.PathLike[str], *, create: bool
) -> Iterator[tuple[Path, int]]:
    """Open and lock down a directory without following its final component."""

    configured = Path(directory).expanduser()
    if create:
        configured.parent.mkdir(parents=True, exist_ok=True)
        try:
            configured.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            pass

    try:
        directory_fd = os.open(configured, _directory_flags())
    except FileNotFoundError as exc:
        raise UnsafeOutputPath("Output directory does not exist.") from exc
    except OSError as exc:
        raise UnsafeOutputPath(
            "Output directory must not be a symbolic link."
        ) from exc

    try:
        opened_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened_stat.st_mode):
            raise UnsafeOutputPath("Output path is not a directory.")
        os.fchmod(directory_fd, PRIVATE_DIRECTORY_MODE)

        try:
            resolved = configured.resolve(strict=True)
            current_stat = os.stat(configured, follow_symlinks=False)
        except (FileNotFoundError, OSError) as exc:
            raise UnsafeOutputPath(
                "Output directory changed during validation."
            ) from exc

        if (opened_stat.st_dev, opened_stat.st_ino) != (
            current_stat.st_dev,
            current_stat.st_ino,
        ):
            raise UnsafeOutputPath(
                "Output directory changed during validation."
            )
        yield resolved, directory_fd
    finally:
        os.close(directory_fd)


def ensure_private_directory(
    directory: str | os.PathLike[str],
) -> Path:
    """Create an owner-only directory or repair an existing directory's mode."""

    _require_secure_output_platform()
    if _posix_primitives_available():
        with _posix_open_private_directory(directory, create=True) as (
            resolved,
            _dir_fd,
        ):
            return resolved
    return _windows_private_directory(directory, create=True)


def _private_file_flags(*, append: bool = False) -> int:
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_APPEND if append else os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


def atomic_write_private_json(
    path: str | os.PathLike[str], payload: Any
) -> Path:
    """Atomically replace a JSON file using a private exclusive temp file."""

    requested = Path(path)
    if requested.name in {"", ".", ".."}:
        raise UnsafeOutputPath("Output filename is invalid.")

    _require_secure_output_platform()
    if not _posix_primitives_available():
        return _windows_atomic_write_private_json(requested, payload)

    with _posix_open_private_directory(requested.parent, create=True) as (
        resolved_directory,
        directory_fd,
    ):
        temporary_name = (
            f".{requested.name}.{secrets.token_hex(8)}.tmp"
        )
        file_fd = os.open(
            temporary_name,
            _private_file_flags(),
            PRIVATE_FILE_MODE,
            dir_fd=directory_fd,
        )
        temporary_stat = os.fstat(file_fd)
        completed = False
        try:
            os.fchmod(file_fd, PRIVATE_FILE_MODE)
            with os.fdopen(file_fd, "w", encoding="utf-8") as output:
                file_fd = -1
                json.dump(payload, output, indent=2, ensure_ascii=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), PRIVATE_FILE_MODE)

            os.rename(
                temporary_name,
                requested.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            completed = True
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            if not completed:
                try:
                    current = os.stat(
                        temporary_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    current = None
                if current is not None and (
                    current.st_dev,
                    current.st_ino,
                ) == (
                    temporary_stat.st_dev,
                    temporary_stat.st_ino,
                ):
                    try:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass

        return resolved_directory / requested.name


def _windows_atomic_write_private_json(requested: Path, payload: Any) -> Path:
    """Write to a hidden temp file, then replace the target atomically.

    ``os.replace`` is the overwriting counterpart of the POSIX ``rename``
    above - a cache entry is meant to be refreshed in place - and because it
    targets a fresh path rather than the existing entry, a symlink sitting at
    the destination is replaced rather than written through.
    """

    directory = _windows_private_directory(requested.parent, create=True)
    destination = directory / requested.name
    _windows_reject_link(
        destination.parent, "Output directory must not be a symbolic link."
    )
    temporary_path = directory / f".{requested.name}.{secrets.token_hex(8)}.tmp"

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOINHERIT", 0)
    file_fd = os.open(temporary_path, flags, PRIVATE_FILE_MODE)

    completed = False
    try:
        with os.fdopen(file_fd, "w", encoding="utf-8") as output:
            file_fd = -1
            json.dump(payload, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination)
        completed = True
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if not completed:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass

    return destination


def open_private_json(
    path: str | os.PathLike[str],
) -> TextIO:
    """Open existing JSON without following links and repair its mode.

    The caller owns and must close the returned text stream.
    """

    requested = Path(path)
    _require_secure_output_platform()
    if not _posix_primitives_available():
        return _windows_open_private_json(requested)

    with _posix_open_private_directory(requested.parent, create=False) as (
        _resolved_directory,
        directory_fd,
    ):
        flags = os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            file_fd = os.open(requested.name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise UnsafeOutputPath(
                "JSON output must be a regular non-link file."
            ) from exc

        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            raise UnsafeOutputPath("JSON output is not a regular file.")
        try:
            os.fchmod(file_fd, PRIVATE_FILE_MODE)
        except Exception:
            os.close(file_fd)
            raise
        return os.fdopen(file_fd, "r", encoding="utf-8")


def _windows_open_private_json(requested: Path) -> TextIO:
    directory = _windows_private_directory(requested.parent, create=False)
    target = directory / requested.name

    entry = _windows_reject_link(
        target, "JSON output must be a regular non-link file."
    )
    if entry is None:
        raise UnsafeOutputPath("JSON output does not exist.")
    if not stat.S_ISREG(entry.st_mode):
        raise UnsafeOutputPath("JSON output is not a regular file.")
    return open(target, "r", encoding="utf-8")


def _windows_open_private_log(requested: Path) -> tuple[Path, int]:
    directory = _windows_private_directory(requested.parent, create=True)
    target = directory / requested.name

    entry = _windows_reject_link(
        target, "Log output must be a regular non-link file."
    )
    if entry is not None and not stat.S_ISREG(entry.st_mode):
        raise UnsafeOutputPath("Log output is not a regular file.")

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOINHERIT", 0)
    try:
        file_fd = os.open(target, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise UnsafeOutputPath(
            "Log output must be a regular non-link file."
        ) from exc
    return directory, file_fd


class PrivateFileHandler(logging.StreamHandler):
    """Append-only logging handler backed by a no-follow owner-only file."""

    def __init__(
        self, filename: str | os.PathLike[str], encoding: str = "utf-8"
    ) -> None:
        requested = Path(filename)
        _require_secure_output_platform()
        if not _posix_primitives_available():
            resolved_directory, file_fd = _windows_open_private_log(requested)
            self.baseFilename = str(resolved_directory / requested.name)
            try:
                stream = os.fdopen(file_fd, "a", encoding=encoding)
            except Exception:
                os.close(file_fd)
                raise
            super().__init__(stream)
            return

        with _posix_open_private_directory(requested.parent, create=True) as (
            resolved_directory,
            directory_fd,
        ):
            try:
                file_fd = os.open(
                    requested.name,
                    _private_file_flags(append=True),
                    PRIVATE_FILE_MODE,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise UnsafeOutputPath(
                    "Log output must be a regular non-link file."
                ) from exc

            try:
                file_stat = os.fstat(file_fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise UnsafeOutputPath(
                        "Log output is not a regular file."
                    )
                os.fchmod(file_fd, PRIVATE_FILE_MODE)
            except Exception:
                os.close(file_fd)
                raise

        self.baseFilename = str(resolved_directory / requested.name)
        try:
            stream = os.fdopen(file_fd, "a", encoding=encoding)
        except Exception:
            os.close(file_fd)
            raise
        super().__init__(stream)

    def close(self) -> None:
        try:
            if self.stream is not None and not self.stream.closed:
                self.flush()
                self.stream.close()
        finally:
            super().close()
