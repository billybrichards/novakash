"""
DDL migration helpers for v5.8 monitor tables.

Extracted from hub/api/v58_monitor.py -- route files should not contain
CREATE TABLE / ALTER TABLE statements.  These are idempotent migration
functions called from hub/main.py lifespan and (defensively) from a
handful of route handlers that need to guarantee the schema exists.

All functions accept a SQLAlchemy AsyncSession and are safe to call
repeatedly -- every statement uses IF NOT EXISTS guards.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def ensure_manual_trades_table(session: AsyncSession) -> None:
    """Create manual_trades table if it doesn't exist.

    POLY-SOT additions (2026-04-11): adds polymarket_confirmed_* columns and
    sot_reconciliation_state so the SOT reconciler in engine/reconciliation
    can stamp every row with the authoritative Polymarket CLOB record.
    Mirrors the margin_engine pattern where exchange API is the SOT.
    """
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS manual_trades (
            id SERIAL PRIMARY KEY,
            trade_id VARCHAR(64) UNIQUE NOT NULL,
            window_ts BIGINT,
            asset VARCHAR(10) DEFAULT 'BTC',
            direction VARCHAR(4) NOT NULL,
            mode VARCHAR(10) NOT NULL,
            entry_price DOUBLE PRECISION NOT NULL,
            gamma_up_price DOUBLE PRECISION,
            gamma_down_price DOUBLE PRECISION,
            stake_usd DOUBLE PRECISION DEFAULT 4.0,
            status VARCHAR(20) DEFAULT 'open',
            outcome_direction VARCHAR(4),
            pnl_usd DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            resolved_at TIMESTAMPTZ
        )
    """))
    # Add order_type column if missing (migration-safe)
    await session.execute(text("""
        ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS order_type VARCHAR(5) DEFAULT 'FAK'
    """))

    # ── POLY-SOT columns: Polymarket CLOB as source-of-truth for manual trades ──
    # See migrations/add_manual_trades_sot_columns.sql for the canonical migration.
    # Each ALTER is independently idempotent so a partial historical apply still
    # converges to the full schema after one more lifespan startup.
    for ddl in (
        "ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_order_id TEXT",
        "ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_status TEXT",
        "ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_fill_price NUMERIC(18,6)",
        "ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_size NUMERIC(18,6)",
        "ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_at TIMESTAMPTZ",
        "ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS polymarket_last_verified_at TIMESTAMPTZ",
        "ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS sot_reconciliation_state TEXT",
        "ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS sot_reconciliation_notes TEXT",
    ):
        await session.execute(text(ddl))
    # Indexes — both partial so they only cost us bytes for rows that have data.
    await session.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_manual_trades_polymarket_order_id "
        "ON manual_trades(polymarket_order_id) WHERE polymarket_order_id IS NOT NULL"
    ))
    await session.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_manual_trades_sot_state "
        "ON manual_trades(sot_reconciliation_state) WHERE sot_reconciliation_state IS NOT NULL"
    ))
    await session.commit()


async def ensure_trades_sot_columns(session: AsyncSession) -> None:
    """POLY-SOT-b — add SOT columns to the existing `trades` table.

    The `trades` table itself is created by hub/db/schema.sql at first
    deployment; this function only ALTERs in the SOT columns added in
    this PR. Mirrors ensure_manual_trades_table's POLY-SOT block but for
    automatic engine trades.

    See migrations/add_trades_sot_columns.sql for the canonical migration.
    Each ALTER is independently idempotent so a partial historical apply
    still converges to the full schema after one more lifespan startup.
    """
    for ddl in (
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_order_id TEXT",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_status TEXT",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_fill_price NUMERIC(18,6)",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_size NUMERIC(18,6)",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_confirmed_at TIMESTAMPTZ",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS polymarket_last_verified_at TIMESTAMPTZ",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS sot_reconciliation_state TEXT",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS sot_reconciliation_notes TEXT",
    ):
        await session.execute(text(ddl))
    await session.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_trades_polymarket_order_id "
        "ON trades(polymarket_order_id) WHERE polymarket_order_id IS NOT NULL"
    ))
    await session.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_trades_sot_state "
        "ON trades(sot_reconciliation_state) WHERE sot_reconciliation_state IS NOT NULL"
    ))
    await session.commit()


async def ensure_manual_trade_snapshots_table(session: AsyncSession) -> None:
    """
    LT-03 — Create manual_trade_snapshots table for operator-vs-engine
    ground-truth analysis.

    Every manual trade placed through /api/v58/manual-trade writes a companion
    row into this table capturing the full decision context at the moment the
    operator clicked: v4 fusion surface, v3 composite, last 5 resolved
    outcomes, macro bias, VPIN, and what the engine's gate pipeline would have
    decided for that same window. After resolution we know whether the
    operator was right, whether the engine was right, and where they disagree.

    JSONB columns let us capture the full surface without forcing a schema
    for every field the decision surface might add in future.
    """
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS manual_trade_snapshots (
            id SERIAL PRIMARY KEY,
            trade_id VARCHAR(64) NOT NULL,
            window_ts BIGINT NOT NULL,
            taken_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            -- Operator input
            operator_rationale TEXT,
            operator_direction CHAR(2) NOT NULL,

            -- Full v4 fusion surface at decision time (complete JSON)
            v4_snapshot JSONB,

            -- v3 composite signal surface (complete JSON)
            v3_snapshot JSONB,

            -- Last 5 resolved window outcomes preceding this decision
            last_5_window_outcomes JSONB,

            -- What the engine's gate pipeline decided for this window
            engine_would_have_done CHAR(5),
            engine_gate_reason VARCHAR(100),
            engine_direction CHAR(2),

            -- VPIN, macro bias snapshot
            vpin NUMERIC(6,4),
            macro_bias VARCHAR(16),
            macro_confidence INTEGER,

            -- Resolution (populated later when the trade resolves)
            resolved_at TIMESTAMPTZ,
            resolved_outcome CHAR(2),
            resolved_pnl_usd NUMERIC(10,4),
            operator_was_right BOOLEAN,
            engine_was_right BOOLEAN,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await session.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_mts_trade_id ON manual_trade_snapshots(trade_id)"
    ))
    await session.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_mts_window_ts ON manual_trade_snapshots(window_ts DESC)"
    ))
    await session.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_mts_taken_at ON manual_trade_snapshots(taken_at DESC)"
    ))
    await session.commit()
