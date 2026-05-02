#!/bin/bash
# Common helpers for cross-compare scripts.
# Run from any box that can reach RDS (Hub box recommended — connection
# already vetted; localhost on the Hub box has direct DB access).

set -euo pipefail

DAYS="${1:-7}"

DB_HOST="${RDS_HOST:-novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com}"
DB_PORT="${RDS_PORT:-5432}"
DB_USER="${RDS_USER:-postgres}"
DB_NAME="${RDS_DB:-novakash}"
DB_PASS="${RDS_PASSWORD:-GuZbKkezYSzX4qGTxnkTynH0AIJy}"

run_psql() {
    PGPASSWORD="$DB_PASS" psql \
        -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        --set ON_ERROR_STOP=1 \
        --pset pager=off \
        --pset border=1 \
        --pset format=aligned \
        -c "SET statement_timeout = '120s';" \
        -c "$1"
}
