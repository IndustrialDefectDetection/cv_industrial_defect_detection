"""Owner-only, race-resistant helpers for generated operational output."""

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


def _require_secure_output_platform() -> None:
    required_dir_fd = (os.open, os.stat, os.unlink, os.rename)
    supported = (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "fchmod")
        and all(function in os.supports_dir_fd for function in required_dir_fd)
        and os.stat in os.supports_follow_symlinks
    )
    if not supported:
        raise UnsupportedOutputPlatform(
            "Secure generated output requires POSIX directory descriptors, "
            "no-follow opens, and owner-only chmod support. This platform "
            "is not supported; no output file was created."
        )


def _directory_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


@contextmanager
def open_private_directory(
    directory: str | os.PathLike[str], *, create: bool
) -> Iterator[tuple[Path, int]]:
    """Open and lock down a directory without following its final component."""

    _require_secure_output_platform()
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

    with open_private_directory(directory, create=True) as (resolved, _dir_fd):
        return resolved


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

    with open_private_directory(requested.parent, create=True) as (
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


def open_private_json(
    path: str | os.PathLike[str],
) -> TextIO:
    """Open existing JSON without following links and repair its mode.

    The caller owns and must close the returned text stream.
    """

    requested = Path(path)
    with open_private_directory(requested.parent, create=False) as (
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


class PrivateFileHandler(logging.StreamHandler):
    """Append-only logging handler backed by a no-follow owner-only file."""

    def __init__(
        self, filename: str | os.PathLike[str], encoding: str = "utf-8"
    ) -> None:
        requested = Path(filename)
        with open_private_directory(requested.parent, create=True) as (
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
