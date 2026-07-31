import os
import re
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from display_security import safe_log_text

# ==========================================
# DATABASE CONFIGURATION
# ==========================================
# Defaults are deliberately local-only. A remote database must be configured
# explicitly and must use certificate- and hostname-verified TLS.
SQLITE_FILE = os.getenv("MES_SQLITE_FILE", "mescopy_v1.db")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validated_host(host):
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


def _is_local_host(host):
    normalized = host.strip().lower()
    return normalized in _LOCAL_HOSTS or normalized.startswith("/")


def _bounded_int(name, default, minimum, maximum):
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

PG_SSLMODE = os.getenv(
    "MES_PG_SSLMODE",
    "disable" if _LOCAL_DATABASE else "verify-full",
).strip().lower()
if not _LOCAL_DATABASE and PG_SSLMODE != "verify-full":
    raise RuntimeError(
        "Remote PostgreSQL requires MES_PG_SSLMODE=verify-full"
    )

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


def _connection_kwargs(dbname=None):
    """Build one consistent, TLS-aware psycopg2 connection configuration."""
    kwargs = {
        "host": PG_HOST,
        "port": PG_PORT,
        "user": PG_USER,
        "dbname": dbname or PG_DBNAME,
        "sslmode": PG_SSLMODE,
        "connect_timeout": PG_CONNECT_TIMEOUT,
    }
    if PG_PASSWORD is not None:
        kwargs["password"] = PG_PASSWORD
    if PG_SSLROOTCERT:
        kwargs["sslrootcert"] = PG_SSLROOTCERT
    return kwargs


def _connect(dbname=None):
    return psycopg2.connect(**_connection_kwargs(dbname))

def ensure_postgres_db_exists():
    """Connects to the default 'postgres' database and creates the target DB if missing."""
    print(f"Checking if PostgreSQL database '{PG_DBNAME}' exists...")
    conn = None
    cursor = None
    try:
        # Connect to the default 'postgres' maintenance database first
        conn = _connect("postgres")

        # need AUTOCOMMIT mode here to run CREATE DATABASE statements
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cursor = conn.cursor()
        
        # Check if our target DB already exists
        cursor.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s;", (PG_DBNAME,))
        exists = cursor.fetchone()
        
        if not exists:
            print(f"Database '{PG_DBNAME}' not found. Creating it now...")
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(PG_DBNAME))
            )
            print(f"Successfully created database '{PG_DBNAME}'!")
        else:
            print(f"Database '{PG_DBNAME}' already exists. Skipping creation step.")
            
        return True
        
    except Exception as e:
        print(
            "Error while checking/creating the database: "
            f"{safe_log_text(e)}"
        )
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

def run_pgloader():
    """Use the built-in copy instead of putting credentials in process argv.

    pgloader requires its destination URI on the command line in this workflow.
    Embedding the password there exposes it to process listings and can echo it
    back in errors. Returning False selects copy_tables_with_python(), which
    uses the same protected psycopg2 connection settings without an external
    process.
    """
    print(
        "\nSkipping pgloader so database credentials never enter process "
        "arguments; using the built-in copy."
    )
    return False

# SQLite is loosely typed; these are the six declared types mes.db actually
# uses. Anything unexpected falls back to TEXT rather than failing the copy.
_SQLITE_TO_PG = {
    "INTEGER": "INTEGER",
    "TEXT": "TEXT",
    "VARCHAR": "TEXT",
    "FLOAT": "DOUBLE PRECISION",
    "BOOLEAN": "BOOLEAN",
    "DATETIME": "TIMESTAMP",
}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validated_identifier(value):
    """Reject metadata-controlled SQL identifiers outside the MES convention."""
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"Unsafe database identifier: {value!r}")
    return value


def copy_tables_with_python():
    """Copy every mes.db table into PostgreSQL without pgloader.

    pgloader has no official Windows build, which left this project unable to
    populate the database at all. The dataset is small (14 tables, ~44k rows),
    so a direct copy is simpler than a toolchain dependency that fights the
    platform.

    Identifiers are created UNQUOTED on purpose: PostgreSQL folds those to
    lower case, which is what the bridge's unquoted queries
    (`SELECT MachineID ... FROM Machines`) resolve to, and what
    RealDictCursor hands back (bridge.py already reads `order["orderid"]`).
    Quoting them as "Machines" would make every existing query fail.
    """
    import sqlite3

    from psycopg2.extras import execute_values

    if not os.path.exists(SQLITE_FILE):
        print(f"Cannot copy: SQLite file '{SQLITE_FILE}' not found.")
        return False

    print(f"\nCopying tables from {SQLITE_FILE} into '{PG_DBNAME}' (no pgloader needed)...")
    lite = None
    pg = None
    copied, mismatched = 0, []
    try:
        lite = sqlite3.connect(SQLITE_FILE)
        lite.row_factory = sqlite3.Row
        pg = _connect()
        tables = [r[0] for r in lite.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

        for table in tables:
            table = _validated_identifier(table)
            cols = list(lite.execute(f"PRAGMA table_info({table})"))
            names = [_validated_identifier(c["name"]) for c in cols]
            bool_idx = {i for i, c in enumerate(cols)
                        if (c["type"] or "").upper() == "BOOLEAN"}

            defs = []
            for c in cols:
                pg_type = _SQLITE_TO_PG.get((c["type"] or "").upper(), "TEXT")
                defs.append(f'{c["name"]} {pg_type}'
                            + (" PRIMARY KEY" if c["pk"] else ""))

            rows = [tuple(r) for r in lite.execute(f'SELECT * FROM "{table}"')]
            # SQLite keeps booleans as 0/1; PostgreSQL's BOOLEAN rejects ints.
            if bool_idx:
                rows = [tuple(bool(v) if i in bool_idx and v is not None else v
                              for i, v in enumerate(row)) for row in rows]

            with pg.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
                cur.execute(f"CREATE TABLE {table} ({', '.join(defs)})")
                if rows:
                    execute_values(
                        cur,
                        f"INSERT INTO {table} ({', '.join(names)}) VALUES %s",
                        rows, page_size=1000)
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                landed = cur.fetchone()[0]

            if landed != len(rows):
                mismatched.append((table, len(rows), landed))
            print(f"  {table:<22} {landed:>7} rows")
            copied += 1

        pg.commit()
    except Exception as exc:
        if pg is not None:
            pg.rollback()
        print(f"Copy failed: {safe_log_text(exc)}")
        return False
    finally:
        if lite is not None:
            lite.close()
        if pg is not None:
            pg.close()

    if mismatched:
        print("Row-count mismatches:", mismatched)
        return False
    print(f"Copied {copied} tables with every row accounted for.")
    return True


def create_contract_tables():
    """Creates the VisionDetections and AgentAlerts tables (CONTRACTS.md §3) in PostgreSQL."""
    print("\nCreating CONTRACTS.md §3 tables (VisionDetections, AgentAlerts) in PostgreSQL...")

    conn = None
    cursor = None
    try:
        conn = _connect()
        cursor = conn.cursor()

        queries = [
            """
            CREATE TABLE IF NOT EXISTS VisionDetections (
                DetectionID     SERIAL PRIMARY KEY,
                Timestamp       TIMESTAMPTZ NOT NULL,
                MachineID       INTEGER NOT NULL,
                OrderID         INTEGER,
                DefectType      TEXT NOT NULL,
                Confidence      DOUBLE PRECISION NOT NULL,
                BBox            TEXT,
                ImageName       TEXT,
                InferenceTimeMs DOUBLE PRECISION
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS AgentAlerts (
                AlertID         SERIAL PRIMARY KEY,
                CreatedAt       TIMESTAMPTZ NOT NULL,
                MachineID       INTEGER NOT NULL,
                OrderID         INTEGER,
                DefectType      TEXT NOT NULL,
                DetectionCount  INTEGER NOT NULL,
                WindowStart     TIMESTAMPTZ NOT NULL,
                WindowEnd       TIMESTAMPTZ NOT NULL,
                Status          TEXT NOT NULL DEFAULT 'pending',
                Report          TEXT,
                CompletedAt     TIMESTAMPTZ
            );
            """
        ]

        for query in queries:
            cursor.execute(query)

        conn.commit()
        print("Tables 'VisionDetections' and 'AgentAlerts' successfully created!")
        return True

    except Exception as e:
        print(f"Error creating contract tables: {safe_log_text(e)}")
        if conn is not None:
            conn.rollback()
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def add_ml_columns():
    """Connects to the target Postgres DB and safely alters the 'defects' table structure."""
    print("\nAdding live-tracking ML columns to the 'defects' table...")
    
    conn = None
    cursor = None
    try:
        # Establish connection to the newly populated target DB
        conn = _connect()
        cursor = conn.cursor()
        
        # SQL commands to append our confidence score and future-proofing image pointer
        alter_queries = [
            "ALTER TABLE defects ADD COLUMN IF NOT EXISTS confidence double precision;",
            "ALTER TABLE defects ADD COLUMN IF NOT EXISTS image_url text;"
        ]
        
        for query in alter_queries:
            cursor.execute(query)
            
        conn.commit()
        print("Columns 'confidence' and 'image_url' successfully attached!")
        
    except Exception as e:
        print(f"Database alteration failed: {safe_log_text(e)}")
        if conn is not None:
            conn.rollback()
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

if __name__ == "__main__":
    # Step 1 is the only hard prerequisite: without the database, nothing else
    # can run.
    if not ensure_postgres_db_exists():
        raise SystemExit(1)

    # Step 2 copies the historical MES tables across. pgloader is disabled in
    # this workflow because its destination URI would expose the password in
    # process arguments, so the protected built-in copy is always selected.
    migrated = run_pgloader()
    if not migrated:
        print("Falling back to the built-in copy...")
        migrated = copy_tables_with_python()

    # Step 3 must NOT depend on step 2. The two contract tables are created
    # directly here and are what the bridge and the analyze_batch seam
    # actually write to - gating them behind pgloader meant that on any
    # machine without it the pipeline had no tables at all and could not run.
    tables_ok = create_contract_tables()

    # Step 4 only makes sense once the MES tables exist (it alters 'defects',
    # which pgloader brings over).
    if migrated:
        add_ml_columns()

    print()
    if migrated and tables_ok:
        print("All steps complete! Your live-tracking database is ready to roll.")
    elif tables_ok:
        print("Partial setup: VisionDetections and AgentAlerts exist, so the "
              "bridge and analyze_batch can run now.")
        print("The historical MES tables were NOT copied, so work-order and "
              "machine lookups will return nothing (OrderID stays NULL) until "
              "the database copy succeeds.")
    else:
        print("Setup failed: the contract tables could not be created.")
        raise SystemExit(1)
