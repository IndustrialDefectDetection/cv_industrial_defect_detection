"""Regression tests for database cleanup on expected query failures."""

import sqlite3
from datetime import datetime

import pandas as pd
import pytest

from strands_agent import MESAgentManager


class FailingCursor:
    def __init__(self):
        self.closed = False

    def execute(self, _query):
        raise RuntimeError("query failed")

    def fetchone(self):
        return None

    def close(self):
        self.closed = True


class Connection:
    def __init__(self):
        self.closed = False
        self.cursor_instance = FailingCursor()

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


def bare_manager(connection):
    manager = MESAgentManager.__new__(MESAgentManager)
    manager.db_backend = "sqlite"
    manager.get_db_connection = lambda: connection
    manager._check_cancelled = lambda: None
    manager.tracer = None
    return manager


def test_safe_query_closes_connection_when_pandas_raises(monkeypatch):
    connection = Connection()
    manager = bare_manager(connection)
    monkeypatch.setattr(
        pd,
        "read_sql_query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    result = manager._execute_safe_query("SELECT 1")

    assert result["success"] is False
    assert connection.closed


def test_data_anchor_closes_cursor_and_connection_on_failure():
    connection = Connection()
    manager = bare_manager(connection)

    anchor = manager._load_data_anchor()

    assert isinstance(anchor, datetime)
    assert connection.cursor_instance.closed
    assert connection.closed


def test_sqlite_agent_connection_is_read_only(tmp_path):
    database = tmp_path / "mes.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")

    manager = MESAgentManager.__new__(MESAgentManager)
    manager.db_backend = "sqlite"
    manager.db_path = str(database)

    connection = manager.get_db_connection()
    try:
        assert connection.execute("SELECT COUNT(*) FROM sample").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO sample VALUES ('not allowed')")
    finally:
        connection.close()


def test_query_chokepoint_rejects_write_statements_before_connecting():
    manager = MESAgentManager.__new__(MESAgentManager)
    manager.db_backend = "sqlite"
    manager.get_db_connection = lambda: pytest.fail("must reject before connecting")
    manager._check_cancelled = lambda: None
    manager.tracer = None

    result = manager._execute_safe_query("DELETE FROM Defects")

    assert result == {
        "success": False,
        "error": "Only read-only SELECT queries are allowed",
        "execution_time_ms": 0.0,
    }


def test_postgres_agent_sessions_default_to_read_only(monkeypatch):
    import psycopg2
    import setupdatabase

    captured = {}
    sentinel = object()
    monkeypatch.setattr(setupdatabase, "_connection_kwargs", lambda: {})

    def connect(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(psycopg2, "connect", connect)
    manager = MESAgentManager.__new__(MESAgentManager)
    manager.db_backend = "postgres"

    assert manager.get_db_connection() is sentinel
    assert "default_transaction_read_only=on" in captured["options"]
