"""Fail-closed loading for project-local files that may contain secrets."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


def load_protected_env(
    path: Path,
    *,
    allowed_names: frozenset[str] | None = None,
) -> bool:
    """Load a protected .env, optionally exporting only an explicit allowlist."""

    env_path = Path(path)
    if env_path.is_symlink():
        raise RuntimeError("Refusing to load a non-regular .env file")
    if not env_path.exists():
        return False
    if not env_path.is_file():
        raise RuntimeError("Refusing to load a non-regular .env file")
    if os.name != "nt":
        mode = stat.S_IMODE(env_path.stat().st_mode)
        if mode & 0o077:
            raise RuntimeError(
                "Refusing to load .env because it is readable or writable "
                "by other local accounts; run chmod 600 on the file"
            )
    if allowed_names is None:
        return load_dotenv(env_path)

    loaded = False
    for name, value in dotenv_values(env_path).items():
        if name in allowed_names and value is not None and name not in os.environ:
            os.environ[name] = value
            loaded = True
    return loaded
