"""Security controls for agent-supplied SQLite queries.

The agent-facing database tools must only expose bounded, read-only queries.
This module keeps the SQL validation and the SQLite connection hardening in one
place so every tool applies the same policy.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Tuple


class ReadOnlyQueryError(ValueError):
    """Raised when a query is outside the permitted read-only SQL subset."""


MAX_SQL_QUERY_BYTES = 100_000
MAX_SQLITE_VALUE_BYTES = 1_000_000

_DENIED_KEYWORDS = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "ATTACH",
        "BEGIN",
        "COMMIT",
        "CREATE",
        "DELETE",
        "DETACH",
        "DROP",
        "END",
        "INSERT",
        "PRAGMA",
        "REINDEX",
        "RELEASE",
        "REPLACE",
        "ROLLBACK",
        "SAVEPOINT",
        "TRUNCATE",
        "UPDATE",
        "VACUUM",
    }
)

_DENIED_FUNCTIONS = frozenset(
    {
        "FTS3_TOKENIZER",
        "LOAD_EXTENSION",
        "READFILE",
        "WRITEFILE",
    }
)

_DENIED_AUTHORIZER_ACTION_NAMES = (
    "SQLITE_ALTER_TABLE",
    "SQLITE_ANALYZE",
    "SQLITE_ATTACH",
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW",
    "SQLITE_DELETE",
    "SQLITE_DETACH",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW",
    "SQLITE_INSERT",
    "SQLITE_PRAGMA",
    "SQLITE_REINDEX",
    "SQLITE_SAVEPOINT",
    "SQLITE_TRANSACTION",
    "SQLITE_UPDATE",
)

_DENIED_AUTHORIZER_ACTIONS = frozenset(
    getattr(sqlite3, name)
    for name in _DENIED_AUTHORIZER_ACTION_NAMES
    if hasattr(sqlite3, name)
)


def _scan_sql(query: str) -> Tuple[List[Tuple[str, int]], int | None]:
    """Return unquoted word tokens with their nesting depth.

    The scanner deliberately ignores quoted values/identifiers and comments,
    while still detecting malformed quoting, unbalanced parentheses, and more
    than one SQL statement. The optional integer is the position of a single
    trailing statement terminator.
    """

    tokens: List[Tuple[str, int]] = []
    index = 0
    depth = 0
    trailing_semicolon: int | None = None
    length = len(query)

    while index < length:
        char = query[index]

        if char.isspace():
            index += 1
            continue

        if query.startswith("--", index):
            newline = query.find("\n", index + 2)
            index = length if newline == -1 else newline + 1
            continue

        if query.startswith("/*", index):
            comment_end = query.find("*/", index + 2)
            if comment_end == -1:
                raise ReadOnlyQueryError("Unterminated SQL comment.")
            index = comment_end + 2
            continue

        if trailing_semicolon is not None:
            raise ReadOnlyQueryError("Only one SQL statement is allowed.")

        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            while index < length:
                if query[index] == quote:
                    if index + 1 < length and query[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise ReadOnlyQueryError("Unterminated quoted SQL value.")
            continue

        if char == "[":
            closing_bracket = query.find("]", index + 1)
            if closing_bracket == -1:
                raise ReadOnlyQueryError("Unterminated quoted SQL identifier.")
            index = closing_bracket + 1
            continue

        if char == "(":
            depth += 1
            index += 1
            continue

        if char == ")":
            depth -= 1
            if depth < 0:
                raise ReadOnlyQueryError("Unmatched parentheses in query.")
            index += 1
            continue

        if char == ";":
            if depth != 0:
                raise ReadOnlyQueryError(
                    "Statement terminators are only allowed at the end of a query."
                )
            trailing_semicolon = index
            index += 1
            continue

        if char.isalpha() or char == "_":
            token_start = index
            index += 1
            while index < length and (
                query[index].isalnum() or query[index] in {"_", "$"}
            ):
                index += 1
            tokens.append((query[token_start:index].upper(), depth))
            continue

        index += 1

    if depth != 0:
        raise ReadOnlyQueryError("Unmatched parentheses in query.")

    return tokens, trailing_semicolon


def validate_read_only_query(query: str) -> str:
    """Validate and normalize a strict, single-statement SELECT/CTE query.

    Only a top-level ``SELECT`` or a ``WITH`` expression whose final top-level
    statement is ``SELECT`` is accepted. Mutating/administrative statements and
    dangerous extension functions are rejected even if embedded in a CTE.
    """

    if not isinstance(query, str):
        raise ReadOnlyQueryError("Query must be a string.")
    if "\x00" in query:
        raise ReadOnlyQueryError("NUL bytes are not allowed in SQL queries.")
    if len(query.encode("utf-8")) > MAX_SQL_QUERY_BYTES:
        raise ReadOnlyQueryError(
            f"Query exceeds the {MAX_SQL_QUERY_BYTES}-byte limit."
        )

    tokens, trailing_semicolon = _scan_sql(query)
    if not tokens:
        raise ReadOnlyQueryError("Query cannot be empty")

    first_token, first_depth = tokens[0]
    if first_depth != 0 or first_token not in {"SELECT", "WITH"}:
        raise ReadOnlyQueryError("Only SELECT queries and read-only CTEs are allowed.")

    for token, _depth in tokens:
        if token in _DENIED_KEYWORDS:
            raise ReadOnlyQueryError(
                f"SQL keyword '{token}' is not allowed in read-only queries."
            )
        if token in _DENIED_FUNCTIONS:
            raise ReadOnlyQueryError(
                f"SQL function '{token}' is not allowed in read-only queries."
            )

    if first_token == "WITH" and not any(
        token == "SELECT" and depth == 0 for token, depth in tokens[1:]
    ):
        raise ReadOnlyQueryError(
            "A read-only CTE must end with a top-level SELECT statement."
        )

    normalized = (
        query[:trailing_semicolon].rstrip()
        if trailing_semicolon is not None
        else query.strip()
    )
    if not normalized:
        raise ReadOnlyQueryError("Query cannot be empty.")
    return normalized


def _read_only_authorizer(
    action_code: int,
    argument_one: str | None,
    argument_two: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    """SQLite authorizer callback that denies writes and unsafe functions."""

    if action_code in _DENIED_AUTHORIZER_ACTIONS:
        return sqlite3.SQLITE_DENY

    if action_code == getattr(sqlite3, "SQLITE_FUNCTION", -1):
        function_name = (argument_two or argument_one or "").upper()
        if function_name in _DENIED_FUNCTIONS:
            return sqlite3.SQLITE_DENY

    return sqlite3.SQLITE_OK


def open_read_only_connection(
    db_path: str | Path, *, lock_timeout_seconds: float = 5.0
) -> sqlite3.Connection:
    """Open an existing SQLite database with layered read-only protections."""

    resolved_path = Path(db_path).expanduser().resolve(strict=True)
    if not resolved_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {resolved_path}")

    database_uri = f"{resolved_path.as_uri()}?mode=ro"
    connection = sqlite3.connect(
        database_uri,
        uri=True,
        timeout=max(0.001, float(lock_timeout_seconds)),
    )
    try:
        set_limit = getattr(connection, "setlimit", None)
        if not callable(set_limit):
            raise sqlite3.NotSupportedError(
                "This Python SQLite runtime cannot enforce result-size limits."
            )
        set_limit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_QUERY_BYTES)
        set_limit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQLITE_VALUE_BYTES)
        connection.execute("PRAGMA query_only = ON")
        connection.set_authorizer(_read_only_authorizer)
    except Exception:
        connection.close()
        raise
    return connection
