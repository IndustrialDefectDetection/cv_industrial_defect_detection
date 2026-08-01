"""Bridge database configuration must make remote TLS non-optional."""

import importlib.util
import os
from pathlib import Path

import pytest


PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "bridge" / "db_config.py"
DATABASE_ENV_NAMES = (
    "MES_PG_HOST",
    "MES_PG_PORT",
    "MES_PG_DBNAME",
    "MES_PG_USER",
    "MES_PG_PASSWORD",
    "MES_PG_SSLMODE",
    "MES_PG_SSLROOTCERT",
    "MES_PG_CONNECT_TIMEOUT",
    "PGSSLMODE",
    "PGSSLROOTCERT",
    "PGCONNECT_TIMEOUT",
)


def _load_config():
    spec = importlib.util.spec_from_file_location(
        "bridge_db_config_security_test",
        CONFIG_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _clear_database_env(monkeypatch):
    for name in DATABASE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_local_defaults_are_local_and_passwordless(monkeypatch):
    _clear_database_env(monkeypatch)

    config = _load_config()

    assert config.PG_HOST == "127.0.0.1"
    assert config.PG_USER == "postgres"
    assert config.PG_PASSWORD is None
    assert config.PG_SSLMODE == "disable"
    assert config.connection_kwargs() == {
        "host": "127.0.0.1",
        "port": 5432,
        "user": "postgres",
        "dbname": "mescopy_v1",
        "sslmode": "disable",
        "connect_timeout": 10,
        "options": (
            "-c statement_timeout=15000 "
            "-c lock_timeout=3000 "
            "-c idle_in_transaction_session_timeout=15000"
        ),
    }


def test_remote_database_fails_without_explicit_credentials(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("MES_PG_HOST", "database.example.test")
    monkeypatch.setenv("MES_PG_USER", "service-user")

    with pytest.raises(RuntimeError, match="MES_PG_PASSWORD"):
        _load_config()


def test_remote_database_enforces_verified_tls(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("MES_PG_HOST", "database.example.test")
    monkeypatch.setenv("MES_PG_USER", "service-user")
    monkeypatch.setenv("MES_PG_PASSWORD", "test-password-not-real")
    monkeypatch.setenv("MES_PG_SSLMODE", "require")

    with pytest.raises(RuntimeError, match="verify-full"):
        _load_config()


def test_remote_database_requires_explicit_ca(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("MES_PG_HOST", "database.example.test")
    monkeypatch.setenv("MES_PG_USER", "service-user")
    monkeypatch.setenv("MES_PG_PASSWORD", "test-password-not-real")
    monkeypatch.setenv("MES_PG_SSLMODE", "verify-full")

    with pytest.raises(RuntimeError, match="MES_PG_SSLROOTCERT"):
        _load_config()


def test_host_lists_cannot_bypass_remote_tls_policy(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv(
        "MES_PG_HOST",
        "/var/run/postgresql,database.example.test",
    )
    monkeypatch.setenv("MES_PG_USER", "service-user")
    monkeypatch.setenv("MES_PG_PASSWORD", "test-password-not-real")
    monkeypatch.setenv("MES_PG_SSLMODE", "disable")

    with pytest.raises(RuntimeError, match="exactly one database host"):
        _load_config()


def test_remote_database_exports_ca_and_timeout(monkeypatch, tmp_path):
    _clear_database_env(monkeypatch)
    root_certificate = tmp_path / "root.crt"
    root_certificate.write_text("test certificate fixture", encoding="utf-8")
    monkeypatch.setenv("MES_PG_HOST", "database.example.test")
    monkeypatch.setenv("MES_PG_USER", "service-user")
    monkeypatch.setenv("MES_PG_PASSWORD", "test-password-not-real")
    monkeypatch.setenv("MES_PG_SSLMODE", "verify-full")
    monkeypatch.setenv("MES_PG_SSLROOTCERT", str(root_certificate))
    monkeypatch.setenv("MES_PG_CONNECT_TIMEOUT", "7")

    config = _load_config()

    assert config.PG_SSLMODE == "verify-full"
    assert config.PG_SSLROOTCERT == str(root_certificate)
    assert config.PG_CONNECT_TIMEOUT == 7
    kwargs = config.connection_kwargs()
    assert kwargs["sslmode"] == "verify-full"
    assert kwargs["sslrootcert"] == str(root_certificate)
    assert kwargs["connect_timeout"] == 7
    assert kwargs["password"] == "test-password-not-real"
