"""
Database configuration for the bridge — PostgreSQL connection to the migrated MES data.

Used by mes_lookups.py, bridge.py, and analyze_batch.py to connect to the
same PostgreSQL database that setupdatabase.py creates.
"""

import os
from pathlib import Path


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validated_host(host: str) -> str:
    """Accept exactly one hostname/IP or one normalized Unix socket path."""

    candidate = host.strip()
    if (
        not candidate
        or "," in candidate
        or any(character.isspace() or ord(character) < 32 for character in candidate)
    ):
        raise RuntimeError("MES_PG_HOST must contain exactly one database host")
    if candidate.startswith("/") and os.path.normpath(candidate) != candidate:
        raise RuntimeError(
            "MES_PG_HOST Unix socket paths must be normalized absolute paths"
        )
    return candidate


def _is_local_host(host: str) -> bool:
    """Return whether a database host is restricted to this machine."""
    normalized = host.strip().lower()
    return normalized in _LOCAL_HOSTS or normalized.startswith("/")


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


# Safe local defaults keep the sample convenient without silently connecting to
# a shared cloud database. Remote databases must supply their own user/password
# and are pinned to certificate- and hostname-verified TLS.
PG_HOST = _validated_host(os.getenv("MES_PG_HOST", "127.0.0.1"))
PG_PORT = _bounded_int("MES_PG_PORT", 5432, 1, 65535)
PG_DBNAME = os.getenv("MES_PG_DBNAME", "mescopy_v1").strip()
_LOCAL_DATABASE = _is_local_host(PG_HOST)

PG_USER = os.getenv("MES_PG_USER", "postgres" if _LOCAL_DATABASE else "").strip()
PG_PASSWORD = os.getenv("MES_PG_PASSWORD") or None

if not PG_DBNAME:
    raise RuntimeError("MES_PG_DBNAME must not be empty")
if not _LOCAL_DATABASE and (not PG_USER or not PG_PASSWORD):
    raise RuntimeError(
        "Remote PostgreSQL requires MES_PG_USER and MES_PG_PASSWORD"
    )

_requested_sslmode = os.getenv(
    "MES_PG_SSLMODE",
    "disable" if _LOCAL_DATABASE else "verify-full",
).strip().lower()
if not _LOCAL_DATABASE and _requested_sslmode != "verify-full":
    raise RuntimeError(
        "Remote PostgreSQL requires MES_PG_SSLMODE=verify-full"
    )
PG_SSLMODE = _requested_sslmode

PG_SSLROOTCERT = os.getenv("MES_PG_SSLROOTCERT", "").strip()
if PG_SSLROOTCERT and not Path(PG_SSLROOTCERT).is_file():
    raise RuntimeError(
        "MES_PG_SSLROOTCERT must point to a readable CA certificate file"
    )
if not _LOCAL_DATABASE and not PG_SSLROOTCERT:
    raise RuntimeError(
        "Remote PostgreSQL requires MES_PG_SSLROOTCERT"
    )

PG_CONNECT_TIMEOUT = _bounded_int("MES_PG_CONNECT_TIMEOUT", 10, 1, 60)
PG_STATEMENT_TIMEOUT_MS = _bounded_int(
    "MES_PG_STATEMENT_TIMEOUT_MS", 15_000, 1_000, 30_000
)
PG_LOCK_TIMEOUT_MS = _bounded_int(
    "MES_PG_LOCK_TIMEOUT_MS", 3_000, 500, 10_000
)

def connection_kwargs() -> dict[str, object]:
    """Return the complete connection policy without ambient libpq overrides."""

    kwargs: dict[str, object] = {
        "host": PG_HOST,
        "port": PG_PORT,
        "user": PG_USER,
        "dbname": PG_DBNAME,
        "sslmode": PG_SSLMODE,
        "connect_timeout": PG_CONNECT_TIMEOUT,
        "options": (
            f"-c statement_timeout={PG_STATEMENT_TIMEOUT_MS} "
            f"-c lock_timeout={PG_LOCK_TIMEOUT_MS} "
            "-c idle_in_transaction_session_timeout="
            f"{PG_STATEMENT_TIMEOUT_MS}"
        ),
    }
    if PG_PASSWORD is not None:
        kwargs["password"] = PG_PASSWORD
    if PG_SSLROOTCERT:
        kwargs["sslrootcert"] = PG_SSLROOTCERT
    return kwargs

# CONTRACTS.md constants
CONF_GATE = 0.80
BATCH_WINDOW_SECONDS = 30
