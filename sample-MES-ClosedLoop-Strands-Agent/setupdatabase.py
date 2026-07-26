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

    # Step 2 copies the historical MES tables across. It needs pgloader, which
    # has no official Windows build, so it is allowed to fail.
    migrated = run_pgloader()

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
