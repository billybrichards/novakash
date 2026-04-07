#!/bin/bash
# Run CLOB audit tables migration on Railway Postgres
# Must run from Montreal VPS only

set -e

DB_URL=$(grep DATABASE_URL .env | cut -d= -f2)

echo "=== Creating CLOB execution audit tables ==="
psql "$DB_URL" < migrations/add_clob_execution_audit_tables.sql

echo "=== Migration complete ==="
echo "Tables created:"
echo "  - clob_execution_log (main execution tracking)"
echo "  - fok_ladder_attempts (individual FOK attempts)"
echo "  - clob_book_snapshots (complete book on every poll)"
echo "  - order_audit_log (all order submissions)"
