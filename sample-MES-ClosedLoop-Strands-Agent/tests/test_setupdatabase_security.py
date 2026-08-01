"""Database setup must fail closed for remote connections."""

import importlib.util
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parent.parent
SETUP_PATH = BACKEND_DIR / "setupdatabase.py"
DATABASE_ENV_NAMES = (
    "MES_PG_HOST",
    "MES_PG_PORT",
    "MES_PG_DBNAME",
    "MES_PG_USER",
    "MES_PG_PASSWORD",
    "MES_PG_SSLMODE",
    "MES_PG_SSLROOTCERT",
    "MES_PG_CONNECT_TIMEOUT",
)


def _load_setupdatabase():
    spec = importlib.util.spec_from_file_location(
        "mes_setupdatabase_security_test",
        SETUP_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _clear_database_env(monkeypatch):
    for name in DATABASE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_local_defaults_do_not_contain_remote_credentials(monkeypatch):
    _clear_database_env(monkeypatch)

    setupdatabase = _load_setupdatabase()
    kwargs = setupdatabase._connection_kwargs()

    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["user"] == "postgres"
    assert "password" not in kwargs
    assert kwargs["sslmode"] == "disable"
    assert kwargs["connect_timeout"] == 10


def test_remote_database_requires_credentials_and_verify_full(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("MES_PG_HOST", "database.example.test")
    monkeypatch.setenv("MES_PG_USER", "service-user")

    with pytest.raises(RuntimeError, match="MES_PG_PASSWORD"):
        _load_setupdatabase()

    monkeypatch.setenv("MES_PG_PASSWORD", "test-password-not-real")
    monkeypatch.setenv("MES_PG_SSLMODE", "require")
    with pytest.raises(RuntimeError, match="verify-full"):
        _load_setupdatabase()


def test_remote_database_requires_explicit_ca(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("MES_PG_HOST", "database.example.test")
    monkeypatch.setenv("MES_PG_USER", "service-user")
    monkeypatch.setenv("MES_PG_PASSWORD", "test-password-not-real")
    monkeypatch.setenv("MES_PG_SSLMODE", "verify-full")

    with pytest.raises(RuntimeError, match="MES_PG_SSLROOTCERT"):
        _load_setupdatabase()


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
        _load_setupdatabase()


def test_remote_database_uses_ca_and_timeout(monkeypatch, tmp_path):
    _clear_database_env(monkeypatch)
    root_certificate = tmp_path / "root.crt"
    root_certificate.write_text("test certificate fixture", encoding="utf-8")
    monkeypatch.setenv("MES_PG_HOST", "database.example.test")
    monkeypatch.setenv("MES_PG_USER", "service-user")
    monkeypatch.setenv("MES_PG_PASSWORD", "test-password-not-real")
    monkeypatch.setenv("MES_PG_SSLMODE", "verify-full")
    monkeypatch.setenv("MES_PG_SSLROOTCERT", str(root_certificate))
    monkeypatch.setenv("MES_PG_CONNECT_TIMEOUT", "7")

    setupdatabase = _load_setupdatabase()
    kwargs = setupdatabase._connection_kwargs()

    assert kwargs["sslmode"] == "verify-full"
    assert kwargs["sslrootcert"] == str(root_certificate)
    assert kwargs["connect_timeout"] == 7


def test_pgloader_path_never_spawns_with_a_password(monkeypatch):
    _clear_database_env(monkeypatch)
    setupdatabase = _load_setupdatabase()

    assert setupdatabase.run_pgloader() is False


def test_metadata_identifiers_are_conservative(monkeypatch):
    _clear_database_env(monkeypatch)
    setupdatabase = _load_setupdatabase()

    assert setupdatabase._validated_identifier("WorkOrders") == "WorkOrders"
    with pytest.raises(ValueError, match="Unsafe database identifier"):
        setupdatabase._validated_identifier('Machines"; DROP TABLE users; --')


def test_database_creation_failure_closes_resources(monkeypatch):
    _clear_database_env(monkeypatch)
    setupdatabase = _load_setupdatabase()

    class Cursor:
        closed = False

        def execute(self, *_args):
            raise RuntimeError("query failed")

        def close(self):
            self.closed = True

    class Connection:
        closed = False

        def __init__(self):
            self.cursor_instance = Cursor()

        def set_isolation_level(self, _level):
            pass

        def cursor(self):
            return self.cursor_instance

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(setupdatabase, "_connect", lambda _dbname=None: connection)

    assert setupdatabase.ensure_postgres_db_exists() is False
    assert connection.cursor_instance.closed
    assert connection.closed
