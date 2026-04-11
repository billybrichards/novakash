"""
CFG-02 — tests for hub/db/config_schema.py.

Verifies:
  1. ensure_config_tables() runs the expected DDL strings.
  2. EXPECTED_TABLES + EXPECTED_INDEXES list every name we promised.
  3. Each DDL fragment is idempotent (uses IF NOT EXISTS or guarded DO).
  4. The CREATE TABLE statements include the columns the spec demanded.

We do NOT spin up a real Postgres instance — the SQL uses postgres-only
syntax (JSONB, TIMESTAMPTZ, DEFERRABLE constraints) so SQLite would
choke. Instead we test that the right SQL is generated and recorded
against a mock session that captures every execute() call. The
"does this actually create real tables" check happens during the hub
boot path, which has logged migration errors as warnings since day 1
and is exercised by the deploy smoke test (CI-01) on every push.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from db.config_schema import (
    CREATE_CONFIG_HISTORY_SQL,
    CREATE_CONFIG_KEYS_SQL,
    CREATE_CONFIG_VALUES_SQL,
    CREATE_CONFIG_VALUES_UNIQUE_SQL,
    CREATE_INDEX_HISTORY_SQL,
    CREATE_INDEX_VALUES_SQL,
    EXPECTED_INDEXES,
    EXPECTED_TABLES,
    ensure_config_tables,
)


def test_expected_tables_list_is_complete():
    """All three CFG-02 tables are in the EXPECTED_TABLES tuple."""
    assert "config_keys" in EXPECTED_TABLES
    assert "config_values" in EXPECTED_TABLES
    assert "config_history" in EXPECTED_TABLES
    assert len(EXPECTED_TABLES) == 3


def test_expected_indexes_list_is_complete():
    """Both indexes named in the spec are in EXPECTED_INDEXES."""
    assert "idx_config_values_key" in EXPECTED_INDEXES
    assert "idx_config_history_key_time" in EXPECTED_INDEXES
    assert len(EXPECTED_INDEXES) == 2


def test_config_keys_ddl_has_required_columns():
    """The config_keys DDL includes every column from the CFG-02 spec.

    These are the exact field names the read endpoints query against.
    A missing column would silently break the API at runtime.
    """
    sql = CREATE_CONFIG_KEYS_SQL
    required_columns = (
        "id",
        "service",
        "key",
        "type",
        "default_value",
        "current_value",
        "description",
        "category",
        "restart_required",
        "editable_via_ui",
        "enum_values",
        "min_value",
        "max_value",
        "created_at",
        "updated_at",
    )
    for col in required_columns:
        assert col in sql, f"config_keys DDL missing column: {col}"
    assert "UNIQUE (service, key)" in sql
    assert "IF NOT EXISTS" in sql  # idempotent guard


def test_config_values_ddl_has_required_columns():
    """The config_values DDL includes every required column."""
    sql = CREATE_CONFIG_VALUES_SQL
    for col in (
        "id",
        "config_key_id",
        "value",
        "set_by",
        "set_at",
        "comment",
        "is_active",
    ):
        assert col in sql, f"config_values DDL missing column: {col}"
    assert "REFERENCES config_keys(id) ON DELETE CASCADE" in sql
    assert "IF NOT EXISTS" in sql


def test_config_values_unique_constraint_is_deferrable():
    """The (config_key_id, is_active) unique constraint must be DEFERRABLE
    so writers can do an in-transaction "deactivate old, insert new" swap
    without tripping the constraint mid-transaction."""
    sql = CREATE_CONFIG_VALUES_UNIQUE_SQL
    assert "UNIQUE (config_key_id, is_active)" in sql
    assert "DEFERRABLE" in sql
    assert "INITIALLY DEFERRED" in sql
    # Wrapped in DO $$ ... $$ idempotency guard so re-running on hub
    # boot doesn't error after the first deploy.
    assert "DO $$" in sql
    assert "pg_constraint" in sql


def test_config_history_ddl_has_required_columns():
    """The config_history DDL includes every audit-trail column."""
    sql = CREATE_CONFIG_HISTORY_SQL
    for col in (
        "id",
        "config_key_id",
        "previous_value",
        "new_value",
        "changed_by",
        "changed_at",
        "comment",
    ):
        assert col in sql, f"config_history DDL missing column: {col}"
    assert "REFERENCES config_keys(id) ON DELETE CASCADE" in sql
    assert "IF NOT EXISTS" in sql


def test_index_ddls_use_if_not_exists():
    """Both indexes are idempotent."""
    assert "IF NOT EXISTS" in CREATE_INDEX_VALUES_SQL
    assert "IF NOT EXISTS" in CREATE_INDEX_HISTORY_SQL
    assert "idx_config_values_key" in CREATE_INDEX_VALUES_SQL
    assert "idx_config_history_key_time" in CREATE_INDEX_HISTORY_SQL
    # The values index has a partial-index WHERE clause to keep it small.
    assert "WHERE is_active = TRUE" in CREATE_INDEX_VALUES_SQL


@pytest.mark.asyncio
async def test_ensure_config_tables_runs_all_six_ddls():
    """ensure_config_tables() executes the six expected DDL statements
    in the order: tables, unique constraint, indexes."""
    session = MagicMock()
    session.execute = AsyncMock()

    await ensure_config_tables(session)

    # 6 statements: 3 tables + 1 unique constraint + 2 indexes
    assert session.execute.call_count == 6

    # Verify the SQL strings, in order, match the module constants.
    # call_args_list[i] is a Call object whose .args[0] is the text() arg.
    executed = [call.args[0] for call in session.execute.call_args_list]
    expected = [
        CREATE_CONFIG_KEYS_SQL,
        CREATE_CONFIG_VALUES_SQL,
        CREATE_CONFIG_VALUES_UNIQUE_SQL,
        CREATE_CONFIG_HISTORY_SQL,
        CREATE_INDEX_VALUES_SQL,
        CREATE_INDEX_HISTORY_SQL,
    ]
    # text() wraps the string — compare its compiled form
    for got, want in zip(executed, expected):
        # SQLAlchemy text() exposes .text on the wrapped object
        got_str = getattr(got, "text", str(got))
        assert got_str == want


@pytest.mark.asyncio
async def test_ensure_config_tables_does_not_commit():
    """ensure_config_tables() leaves commit responsibility to the caller.

    The hub lifespan calls session.commit() between table creation and
    seed insertion so the seed sees a stable schema. We assert here
    that the function itself doesn't issue an unwanted commit.
    """
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await ensure_config_tables(session)

    session.commit.assert_not_called()
