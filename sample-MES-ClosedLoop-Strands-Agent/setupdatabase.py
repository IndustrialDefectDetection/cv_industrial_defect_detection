import os
import subprocess
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# ==========================================
# CONFIGURATION
# ==========================================
SQLITE_FILE = "mescopy_v1.db"  # Replace with your actual SQLite file path

PG_HOST = "localhost"
PG_PORT = "5432"
PG_USER = ""   # replace with username
PG_PASSWORD = ""     # replace with password
PG_DBNAME = "mescopy_v1"          # target database name

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
        print("Error: 'pgloader' utility is not installed or not in your system PATH.")
        print("Quick fix: Run 'brew install pgloader' on your Mac.")
        return False

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
    # Step 1: check database
    if ensure_postgres_db_exists():
        # Step 2: Stream all the tables and historical data across
        if run_pgloader():
            # Step 3: Mutate the defects table layout for the live camera values
            add_ml_columns()
            print("\nAll steps complete! Your live-tracking database is ready to roll.")
