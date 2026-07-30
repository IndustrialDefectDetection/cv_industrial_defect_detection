"""
Database configuration for the bridge — PostgreSQL connection to the migrated MES data.

Used by mes_lookups.py, bridge.py, and analyze_batch.py to connect to the
same PostgreSQL database that setupdatabase.py creates.
"""

import os

# PostgreSQL connection parameters — override via environment variables.
# Defaults point at the Supabase project so the bridge works out of the box
# after setupdatabase.py has been run.  To target a local Postgres instead:
#   MES_PG_HOST=localhost MES_PG_PORT=5432 MES_PG_USER=... uvicorn ...
PG_HOST     = os.getenv("MES_PG_HOST",     "aws-0-ca-central-1.pooler.supabase.com")
PG_PORT     = int(os.getenv("MES_PG_PORT", "6543"))
PG_USER     = os.getenv("MES_PG_USER",     "postgres.isdhddsgfuzvymrxfiox")
PG_PASSWORD = os.getenv("MES_PG_PASSWORD", "cv_industrial_defect_detection")
PG_DBNAME   = os.getenv("MES_PG_DBNAME",   "postgres")

# CONTRACTS.md constants
CONF_GATE = 0.80
BATCH_WINDOW_SECONDS = 30