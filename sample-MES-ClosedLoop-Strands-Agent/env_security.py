"""Fail-closed loading for the project-local file that contains credentials."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_CROSS_SERVICE_SECRET_NAMES = frozenset({
    "ANTHROPIC_API_KEY",
    "AUTH_TRUSTED_PROXY_SECRET",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "BETTER_AUTH_SECRET",
    "DATABASE_CA_CERT",
    "DATABASE_URL",
    "GOOGLE_CLIENT_SECRET",
    "MES_PG_PASSWORD",
})


def load_protected_env(
    path: Path,
    *,
    allowed_names: frozenset[str] | None = None,
) -> bool:
    """Load a protected .env, optionally exporting only an explicit allowlist."""
    env_path = Path(path)
    if not env_path.exists():
        return False
    if env_path.is_symlink() or not env_path.is_file():
        raise RuntimeError("Refusing to load a non-regular .env file")
    if os.name != "nt":
        mode = stat.S_IMODE(env_path.stat().st_mode)
        if mode & 0o077:
            raise RuntimeError(
                "Refusing to load .env because it is readable or writable "
                "by other local accounts; run startup.py to repair it"
            )
    if allowed_names is None:
        return load_dotenv(env_path)

    loaded = False
    for name, value in dotenv_values(env_path).items():
        if name in allowed_names and value is not None and name not in os.environ:
            os.environ[name] = value
            loaded = True
    return loaded


def remove_cross_service_secrets(
    *,
    allowed_names: frozenset[str] = frozenset(),
) -> None:
    """Remove known credentials that the current low-privilege process does not use."""

    for name in _CROSS_SERVICE_SECRET_NAMES - allowed_names:
        os.environ.pop(name, None)
