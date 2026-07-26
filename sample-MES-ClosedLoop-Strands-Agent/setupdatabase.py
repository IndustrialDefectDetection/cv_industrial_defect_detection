import os
import subprocess
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# ==========================================
# CONFIGURATION
# ==========================================
# Read from the same MES_PG_* environment variables the bridge uses
# (industrial-data-store-simulation-chatbot/bridge/db_config.py), so the
# migration and the services that read the result cannot drift apart.
# Previously these were hardcoded empty strings, which no server accepts -
# meaning this script could never have run as committed.
SQLITE_FILE = os.getenv("MES_SQLITE_FILE", "mes.db")  # source db in this folder

PG_HOST = os.getenv("MES_PG_HOST", "localhost")
PG_PORT = os.getenv("MES_PG_PORT", "5432")
PG_USER = os.getenv("MES_PG_USER", "postgres")
PG_PASSWORD = os.getenv("MES_PG_PASSWORD", "postgres")
PG_DBNAME = os.getenv("MES_PG_DBNAME", "mescopy_v1")  # target database name

def ensure_postgres_db_exists():
    """Connects to the default 'postgres' database and creates the target DB if missing."""
    print(f"Checking if PostgreSQL database '{PG_DBNAME}' exists...")
    try:
        # Connect to the default 'postgres' maintenance database first
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            dbname="postgres"
        )

        # need AUTOCOMMIT mode here to run CREATE DATABASE statements
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cursor = conn.cursor()
        
        # Check if our target DB already exists
        cursor.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s;", (PG_DBNAME,))
        exists = cursor.fetchone()
        
        if not exists:
            print(f"Database '{PG_DBNAME}' not found. Creating it now...")
            cursor.execute(f'CREATE DATABASE "{PG_DBNAME}";')
            print(f"Successfully created database '{PG_DBNAME}'!")
        else:
            print(f"Database '{PG_DBNAME}' already exists. Skipping creation step.")
            
        cursor.close()
        conn.close()
        return True
        
    except Exception as e:
        print(f"Error while checking/creating the database: {e}")
        return False

def run_pgloader():
    """Uses pgloader to migrate the SQLite schema and data into PostgreSQL."""
    print(f"\nStarting SQLite to PostgreSQL migration via pgloader...")
    
    if not os.path.exists(SQLITE_FILE):
        print(f"Error: SQLite file '{SQLITE_FILE}' not found at path: {SQLITE_FILE}")
        return False

    # Construct the target PostgreSQL URI string
    pg_uri = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DBNAME}"
    command = ["pgloader", SQLITE_FILE, pg_uri]
    
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        print("Data migration completed by pgloader!")
        print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print("Migration failed during pgloader execution:")
        print(e.stderr)
        return False
    except FileNotFoundError:
        print("'pgloader' is not installed or not on PATH — skipping the MES data copy.")
        print("  macOS:   brew install pgloader")
        print("  Linux:   apt install pgloader")
        print("  Windows: no official build; use WSL, Docker "
              "(ghcr.io/dimitri/pgloader), or copy the tables another way.")
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
    lite = sqlite3.connect(SQLITE_FILE)
    lite.row_factory = sqlite3.Row
    pg = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                          password=PG_PASSWORD, dbname=PG_DBNAME)

    copied, mismatched = 0, []
    try:
        tables = [r[0] for r in lite.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

        for table in tables:
            cols = list(lite.execute(f'PRAGMA table_info("{table}")'))
            names = [c["name"] for c in cols]
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
        pg.rollback()
        print(f"Copy failed: {exc}")
        return False
    finally:
        lite.close()
        pg.close()

    if mismatched:
        print("Row-count mismatches:", mismatched)
        return False
    print(f"Copied {copied} tables with every row accounted for.")
    return True


def create_contract_tables():
    """Creates the VisionDetections and AgentAlerts tables (CONTRACTS.md §3) in PostgreSQL."""
    print("\nCreating CONTRACTS.md §3 tables (VisionDetections, AgentAlerts) in PostgreSQL...")

    try:
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            dbname=PG_DBNAME
        )
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
        print(f"Error creating contract tables: {e}")
        if 'conn' in locals():
            conn.rollback()
        return False
    finally:
        if 'conn' in locals():
            cursor.close()
            conn.close()


def add_ml_columns():
    """Connects to the target Postgres DB and safely alters the 'defects' table structure."""
    print("\nAdding live-tracking ML columns to the 'defects' table...")
    
    try:
        # Establish connection to the newly populated target DB
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            dbname=PG_DBNAME
        )
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
        print(f"Database alteration failed: {e}")
        if 'conn' in locals():
            conn.rollback()
    finally:
        if 'conn' in locals():
            cursor.close()
            conn.close()

if __name__ == "__main__":
    # Step 1 is the only hard prerequisite: without the database, nothing else
    # can run.
    if not ensure_postgres_db_exists():
        raise SystemExit(1)

    # Step 2 copies the historical MES tables across. Prefer pgloader when it
    # is installed; fall back to the built-in Python copy, which needs no
    # external tooling and works on Windows.
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
              "pgloader runs.")
    else:
        print("Setup failed: the contract tables could not be created.")
        raise SystemExit(1)
