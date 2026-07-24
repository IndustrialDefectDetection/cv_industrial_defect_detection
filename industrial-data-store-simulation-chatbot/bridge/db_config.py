"""
Database configuration for the bridge — PostgreSQL connection to the migrated MES data.

Used by mes_lookups.py, bridge.py, and analyze_batch.py to connect to the
same PostgreSQL database that setupdatabase.py creates.
"""

import os

# PostgreSQL connection parameters — override via environment variables
PG_HOST = os.getenv("MES_PG_HOST", "localhost")
PG_PORT = int(os.getenv("MES_PG_PORT", "5432"))
PG_USER = os.getenv("MES_PG_USER", "")
PG_PASSWORD = os.getenv("MES_PG_PASSWORD", "")
PG_DBNAME = os.getenv("MES_PG_DBNAME", "mescopy_v1")

# CONTRACTS.md constants
CONF_GATE = 0.80
BATCH_WINDOW_SECONDS = 30