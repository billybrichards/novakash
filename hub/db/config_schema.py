"""
CFG-02 — DB-backed config schema (config_keys / config_values / config_history).

Three tables that together replace the .env-on-host config workflow with a
single, audit-trailed, hot-reloadable source of truth. This module owns the
DDL only — write endpoints land in CFG-04 and the service-side loaders land
in CFG-07/CFG-08.

Tables:
  - config_keys     — schema registry: one row per (service, key) tuple,
                      describes the key's type, default, range, category,
                      and whether it's editable through the UI.
  - config_values   — current value for each key. UPSERTs from POST writes.
                      One active row per key (enforced via UNIQUE).
  - config_history  — append-only audit log of every change.

Lifecycle: hub/main.py::lifespan calls ensure_config_tables() on startup,
the same way it calls ensure_manual_trades_table() and the inline migrations
for trading_configs/notes/system_state.

This file does NOT touch the existing trading_configs table. The two
schemas coexist for the duration of the migration; CFG-10 decides whether
to mothball trading_configs once the cutover is complete.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


# ─── DDL statements ──────────────────────────────────────────────────────────
#
# Each CREATE TABLE / CREATE INDEX is a standalone string so the migration
# can run them sequentially and report the failing one if anything blows up.
# All statements use IF NOT EXISTS so the migration is idempotent and safe
# to run on every hub boot, the same pattern the existing notes / system_state
# / manual_trades migrations follow.

CREATE_CONFIG_KEYS_SQL = """
CREATE TABLE IF NOT EXISTS config_keys (
    id SERIAL PRIMARY KEY,
    service TEXT NOT NULL,
    key TEXT NOT NULL,
    type TEXT NOT NULL,
    default_value TEXT,
    current_value TEXT,
    description TEXT,
    category TEXT,
    restart_required BOOLEAN NOT NULL DEFAULT FALSE,
    editable_via_ui BOOLEAN NOT NULL DEFAULT TRUE,
    enum_values JSONB,
    min_value TEXT,
    max_value TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (service, key)
)
"""

CREATE_CONFIG_VALUES_SQL = """
CREATE TABLE IF NOT EXISTS config_values (
    id SERIAL PRIMARY KEY,
    config_key_id INTEGER NOT NULL REFERENCES config_keys(id) ON DELETE CASCADE,
    value TEXT,
    set_by TEXT,
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    comment TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
)
"""

# UNIQUE on (config_key_id, is_active) is the constraint that guarantees
# at most one ACTIVE row per key. DEFERRABLE INITIALLY DEFERRED lets writers
# do an "insert new active, mark old inactive" sequence inside one transaction
# without hitting the UNIQUE during the intermediate state. This is how
# CFG-04 will implement updates without losing the audit trail in
# config_values itself (config_history is the canonical history).
CREATE_CONFIG_VALUES_UNIQUE_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'config_values_active_unique'
    ) THEN
        ALTER TABLE config_values
        ADD CONSTRAINT config_values_active_unique
        UNIQUE (config_key_id, is_active)
        DEFERRABLE INITIALLY DEFERRED;
    END IF;
END$$
"""

CREATE_CONFIG_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS config_history (
    id SERIAL PRIMARY KEY,
    config_key_id INTEGER NOT NULL REFERENCES config_keys(id) ON DELETE CASCADE,
    previous_value TEXT,
    new_value TEXT,
    changed_by TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    comment TEXT
)
"""

CREATE_INDEX_VALUES_SQL = """
CREATE INDEX IF NOT EXISTS idx_config_values_key
ON config_values (config_key_id)
WHERE is_active = TRUE
"""

CREATE_INDEX_HISTORY_SQL = """
CREATE INDEX IF NOT EXISTS idx_config_history_key_time
ON config_history (config_key_id, changed_at DESC)
"""


# Tables we expect to exist after a successful migration. Used by the test
# in hub/tests/test_config_schema.py to assert all three landed.
EXPECTED_TABLES = ("config_keys", "config_values", "config_history")
EXPECTED_INDEXES = ("idx_config_values_key", "idx_config_history_key_time")


async def ensure_config_tables(session: AsyncSession) -> None:
    """Create the CFG-02 config_* tables and indexes if missing.

    Idempotent — safe to run on every hub startup. Mirrors the existing
    pattern of ensure_manual_trades_table() in v58_monitor.py and the
    inline DDL in hub/main.py::lifespan.

    Args:
        session: An open AsyncSession. Caller is responsible for commit().

    Raises:
        Any DB error from the underlying execute() — caller in main.py
        catches and logs at WARN so a migration error doesn't take the hub
        down.
    """
    # Tables first, then the deferrable unique constraint, then indexes.
    # The constraint must come after the table is created. Indexes can
    # come last because they reference existing columns.
    await session.execute(text(CREATE_CONFIG_KEYS_SQL))
    await session.execute(text(CREATE_CONFIG_VALUES_SQL))
    await session.execute(text(CREATE_CONFIG_VALUES_UNIQUE_SQL))
    await session.execute(text(CREATE_CONFIG_HISTORY_SQL))
    await session.execute(text(CREATE_INDEX_VALUES_SQL))
    await session.execute(text(CREATE_INDEX_HISTORY_SQL))
    log.info(
        "config_schema.ensured",
        tables=list(EXPECTED_TABLES),
        indexes=list(EXPECTED_INDEXES),
    )
