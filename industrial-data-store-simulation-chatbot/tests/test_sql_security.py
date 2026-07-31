"""Focused tests for the agent-facing read-only SQLite boundary."""

from __future__ import annotations

import sqlite3

import pytest

from app_factory.shared import database as database_module
from app_factory.mes_agents.tools.database_tools import _validate_query
from app_factory.production_meeting_agents.tools.database_tools import (
    _validate_production_query,
)
from app_factory.shared.database import DatabaseManager
from app_factory.shared.sql_security import (
    ReadOnlyQueryError,
    open_read_only_connection,
    validate_read_only_query,
)


@pytest.fixture
def test_database(tmp_path):
    database_path = tmp_path / "security-test.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, score REAL)"
        )
        connection.executemany(
            "INSERT INTO items (name, score) VALUES (?, ?)",
            [
                ("alpha", 1.125),
                ("beta", 2.5),
                ("gamma", 3.75),
            ],
        )
    return database_path


@pytest.mark.parametrize(
    "query",
    [
        "PRAGMA table_info(items)",
        "ATTACH DATABASE '/tmp/other.db' AS other",
        "VACUUM",
        "INSERT INTO items (name) VALUES ('escape')",
        "UPDATE items SET name = 'escape'",
        "DELETE FROM items",
        "REPLACE INTO items (id, name) VALUES (1, 'escape')",
        "CREATE TABLE escaped (id INTEGER)",
        "ALTER TABLE items ADD COLUMN escaped INTEGER",
        "DROP TABLE items",
        "SELECT 1; SELECT 2",
        "SELECT 1;;",
        "WITH changed AS (SELECT 1) DELETE FROM items",
        "WITH changed AS (SELECT 1) UPDATE items SET name = 'escape'",
        "SELECT load_extension('/tmp/extension')",
    ],
)
def test_strict_validator_rejects_unsafe_sql(query):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id, name FROM items ORDER BY id",
        (
            "WITH high_scores AS ("
            "SELECT name, score FROM items WHERE score > 2"
            ") SELECT name FROM high_scores ORDER BY name"
        ),
        (
            "WITH RECURSIVE sequence(value) AS ("
            "VALUES (1) UNION ALL "
            "SELECT value + 1 FROM sequence WHERE value < 3"
            ") SELECT value FROM sequence"
        ),
        "SELECT ';' AS punctuation /* ; DROP TABLE items */;",
        "SELECT 'DELETE FROM items' AS harmless_text",
    ],
)
def test_strict_validator_accepts_selects_and_read_only_ctes(query):
    validated = validate_read_only_query(query)
    assert validated
    assert not validated.rstrip().endswith(";")


@pytest.mark.parametrize("validator", [_validate_query, _validate_production_query])
def test_both_agent_tool_validators_use_the_strict_policy(validator):
    assert validator("SELECT id FROM items")["valid"] is True
    assert (
        validator(
            "WITH selected AS (SELECT id FROM items) "
            "SELECT id FROM selected"
        )["valid"]
        is True
    )

    for query in (
        "PRAGMA table_info(items)",
        "ATTACH DATABASE '/tmp/other.db' AS other",
        "VACUUM",
        "DELETE FROM items",
        "SELECT 1; SELECT 2",
    ):
        result = validator(query)
        assert result["valid"] is False
        assert result["error"]


def test_read_only_executor_returns_bounded_results(test_database):
    manager = DatabaseManager(str(test_database))

    result = manager.execute_read_only_query(
        "SELECT id, name, score FROM items ORDER BY id",
        max_rows=2,
    )

    assert result["success"] is True
    assert result["column_names"] == ["id", "name", "score"]
    assert result["row_count"] == 2
    assert [row["name"] for row in result["rows"]] == ["alpha", "beta"]
    assert result["truncated"] is True
    assert result["max_rows"] == 2


def test_read_only_executor_supports_ctes(test_database):
    manager = DatabaseManager(str(test_database))

    result = manager.execute_read_only_query(
        "WITH chosen AS (SELECT name FROM items WHERE score >= 2.5) "
        "SELECT name FROM chosen ORDER BY name"
    )

    assert result["success"] is True
    assert result["rows"] == [{"name": "beta"}, {"name": "gamma"}]
    assert result["truncated"] is False


def test_read_only_connection_denies_writes_even_without_the_validator(
    test_database,
):
    connection = open_read_only_connection(test_database)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("INSERT INTO items (name) VALUES ('blocked')")
    finally:
        connection.close()

    with sqlite3.connect(test_database) as verification_connection:
        row_count = verification_connection.execute(
            "SELECT COUNT(*) FROM items"
        ).fetchone()[0]
    assert row_count == 3


def test_read_only_executor_interrupts_expensive_queries(test_database):
    manager = DatabaseManager(str(test_database))

    result = manager.execute_read_only_query(
        """
        WITH RECURSIVE counter(value) AS (
            SELECT 1
            UNION ALL
            SELECT value + 1 FROM counter WHERE value < 100000000
        )
        SELECT SUM(value) FROM counter
        """,
        timeout_seconds=0.001,
    )

    assert result["success"] is False
    assert "timeout" in result["error"].lower()


def test_read_only_executor_rejects_oversized_scalar_allocation(test_database):
    manager = DatabaseManager(str(test_database))

    result = manager.execute_read_only_query(
        "SELECT zeroblob(1000000000) AS oversized"
    )

    assert result["success"] is False
    assert result["error"] == (
        "The database could not complete the read-only query."
    )


def test_read_only_executor_caps_cumulative_result_bytes(
    test_database, monkeypatch
):
    monkeypatch.setattr(
        database_module,
        "MAX_READ_ONLY_RESULT_BYTES",
        32 * 1024,
    )
    manager = DatabaseManager(str(test_database))

    result = manager.execute_read_only_query(
        """
        WITH RECURSIVE rows(value) AS (
            SELECT 1
            UNION ALL
            SELECT value + 1 FROM rows WHERE value < 100
        )
        SELECT zeroblob(4096) AS payload FROM rows
        """,
        max_rows=100,
    )

    assert result["success"] is True
    assert 0 < result["row_count"] < 100
    assert result["truncated"] is True
    assert result["max_result_bytes"] == 32 * 1024


def test_validator_bounds_query_text_before_sqlite(test_database):
    query = "SELECT 1 /*" + ("x" * 100_000) + "*/"

    with pytest.raises(ReadOnlyQueryError, match="byte limit"):
        validate_read_only_query(query)


def test_read_only_executor_does_not_disclose_database_paths(tmp_path):
    missing_database = tmp_path / "private" / "missing.db"
    manager = DatabaseManager(str(missing_database))

    result = manager.execute_read_only_query("SELECT 1")

    assert result["success"] is False
    assert result["error"] == (
        "The database could not complete the read-only query."
    )
    assert str(tmp_path) not in result["error"]


def test_schema_introspection_quotes_database_owned_table_names(tmp_path):
    database_path = tmp_path / "untrusted-schema.db"
    unusual_name = 'metrics"; DROP TABLE safe_table; --'
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE safe_table (id INTEGER)")
        quoted_name = '"' + unusual_name.replace('"', '""') + '"'
        connection.execute(f"CREATE TABLE {quoted_name} (value TEXT)")
        connection.execute(
            f"INSERT INTO {quoted_name} (value) VALUES (?)", ("sample",)
        )

    schema = DatabaseManager(str(database_path)).get_schema()

    assert schema[unusual_name]["row_count"] == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'safe_table'"
        ).fetchone()
