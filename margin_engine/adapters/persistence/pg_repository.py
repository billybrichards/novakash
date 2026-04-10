"""
PostgreSQL position repository — persists margin positions to Railway DB.

Uses asyncpg directly (same pattern as the engine's db_client.py).
Table: margin_positions (created by migration).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import PositionRepository
from margin_engine.domain.value_objects import (
    ExitReason,
    Money,
    PositionState,
    Price,
    StopLevel,
    TradeSide,
)

logger = logging.getLogger(__name__)

UPSERT_SQL = """
INSERT INTO margin_positions
    (id, asset, side, state, leverage,
     entry_price, notional, collateral,
     stop_loss_price, take_profit_price,
     exit_price, exit_reason, realised_pnl,
     opened_at, closed_at,
     entry_signal_score, entry_timescale,
     entry_order_id, exit_order_id,
     entry_commission, exit_commission,
     venue, strategy_version,
     hold_clock_anchor, continuation_count, last_continuation_ts, last_continuation_p_up,
     v4_entry_regime, v4_entry_macro_bias, v4_entry_macro_confidence,
     v4_entry_expected_move_bps, v4_entry_composite_v3, v4_entry_consensus_safe,
     v4_entry_window_close_ts, v4_snapshot_ts_at_entry)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23,
        $24, $25, $26, $27,
        $28, $29, $30,
        $31, $32, $33,
        $34, $35)
ON CONFLICT (id) DO UPDATE SET
    state = $4,
    exit_price = $11,
    exit_reason = $12,
    realised_pnl = $13,
    closed_at = $15,
    exit_order_id = $19,
    exit_commission = $21,
    -- Continuation fields are mutable across the position's lifetime,
    -- so refresh on every save. Other new columns (v4_*) are write-once
    -- at entry and don't belong in the ON CONFLICT update.
    hold_clock_anchor = $24,
    continuation_count = $25,
    last_continuation_ts = $26,
    last_continuation_p_up = $27
"""

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS margin_positions (
    id TEXT PRIMARY KEY,
    asset TEXT NOT NULL DEFAULT 'BTC',
    side TEXT NOT NULL,
    state TEXT NOT NULL,
    leverage INT NOT NULL DEFAULT 5,
    entry_price REAL,
    notional REAL,
    collateral REAL,
    stop_loss_price REAL,
    take_profit_price REAL,
    exit_price REAL,
    exit_reason TEXT,
    realised_pnl REAL DEFAULT 0,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    entry_signal_score REAL,
    entry_timescale TEXT,
    entry_order_id TEXT,
    exit_order_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_margin_pos_state ON margin_positions(state);
CREATE INDEX IF NOT EXISTS idx_margin_pos_opened ON margin_positions(opened_at);
"""

# Additive migrations applied on every boot. Safe because:
#   - ADD COLUMN IF NOT EXISTS is idempotent
#   - CREATE INDEX IF NOT EXISTS is idempotent
#   - All new columns have defaults or allow NULL
# Legacy rows stay NULL on the new columns — the _row_to_position layer
# defaults them sensibly ("binance" / "v1-composite" / 0 commission).
ADDITIVE_MIGRATIONS_SQL = (
    # PR #10 additions
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS entry_commission REAL DEFAULT 0",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS exit_commission REAL DEFAULT 0",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS venue TEXT",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS strategy_version TEXT",
    # Partial index for the Trade Timeline tab's "most recent closed" query.
    # Partial = small footprint, only indexes the rows we actually read.
    "CREATE INDEX IF NOT EXISTS idx_margin_pos_closed "
    "ON margin_positions(closed_at DESC) WHERE state = 'CLOSED'",
    # ── PR B additions: re-prediction continuation state ──
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS hold_clock_anchor TIMESTAMPTZ",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS continuation_count INT DEFAULT 0",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS last_continuation_ts TIMESTAMPTZ",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS last_continuation_p_up REAL",
    # ── PR B additions: v4 audit snapshot at entry ──
    # Frozen copy of the v4 fields that drove the entry decision. Enables
    # post-trade analysis like "did regime actually change during the
    # hold?" and "was macro bullish at entry, bearish at exit?".
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_regime TEXT",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_macro_bias TEXT",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_macro_confidence INT",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_expected_move_bps REAL",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_composite_v3 REAL",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_consensus_safe BOOLEAN",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_window_close_ts BIGINT",
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_snapshot_ts_at_entry DOUBLE PRECISION",
)


class PgPositionRepository(PositionRepository):
    """asyncpg-backed position repository."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_table(self) -> None:
        """
        Create table if it doesn't exist AND run additive migrations.

        Idempotent on every boot. Safe on fresh DBs (CREATE_TABLE_SQL wins)
        and on existing DBs (ALTER TABLE ADD COLUMN IF NOT EXISTS no-ops).
        """
        async with self._pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
            for migration in ADDITIVE_MIGRATIONS_SQL:
                await conn.execute(migration)
        logger.info(
            "margin_positions table ensured (additive migrations: %d statements)",
            len(ADDITIVE_MIGRATIONS_SQL),
        )

    async def save(self, position: Position) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                UPSERT_SQL,
                # $1-$19 — original 19 params
                position.id,
                position.asset,
                position.side.value,
                position.state.value,
                position.leverage,
                position.entry_price.value if position.entry_price else None,
                position.notional.amount if position.notional else None,
                position.collateral.amount if position.collateral else None,
                position.stop_loss.price if position.stop_loss else None,
                position.take_profit.price if position.take_profit else None,
                position.exit_price.value if position.exit_price else None,
                position.exit_reason.value if position.exit_reason else None,
                position.realised_pnl,
                _ts(position.opened_at),
                _ts(position.closed_at),
                position.entry_signal_score,
                position.entry_timescale,
                position.entry_order_id,
                position.exit_order_id,
                # $20-$23 — PR #10 additions
                position.entry_commission,
                position.exit_commission,
                position.venue,
                position.strategy_version,
                # $24-$27 — PR B continuation state (mutable)
                _ts(position.hold_clock_anchor),
                position.continuation_count,
                _ts(position.last_continuation_ts),
                position.last_continuation_p_up,
                # $28-$35 — PR B v4 audit snapshot (write-once at entry)
                position.v4_entry_regime,
                position.v4_entry_macro_bias,
                position.v4_entry_macro_confidence,
                position.v4_entry_expected_move_bps,
                position.v4_entry_composite_v3,
                position.v4_entry_consensus_safe,
                position.v4_entry_window_close_ts,
                position.v4_snapshot_ts_at_entry,
            )

    async def get_open_positions(self) -> list[Position]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM margin_positions WHERE state = 'OPEN' ORDER BY opened_at"
            )
        return [self._row_to_position(r) for r in rows]

    async def get_by_id(self, position_id: str) -> Optional[Position]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM margin_positions WHERE id = $1", position_id
            )
        return self._row_to_position(row) if row else None

    async def get_closed_today(self) -> list[Position]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM margin_positions WHERE state = 'CLOSED' AND closed_at >= CURRENT_DATE ORDER BY closed_at"
            )
        return [self._row_to_position(r) for r in rows]

    @staticmethod
    def _row_to_position(row) -> Position:
        # Legacy row handling: all PR #10 and PR B columns fall back
        # to sensible defaults when the row was written before the
        # corresponding migration shipped. This lets the Trade Timeline UI
        # render historical data cleanly without any backfill job.
        hca_col = _safe_get(row, "hold_clock_anchor", None)
        lct_col = _safe_get(row, "last_continuation_ts", None)

        p = Position(
            id=row["id"],
            asset=row["asset"],
            side=TradeSide(row["side"]),
            state=PositionState(row["state"]),
            leverage=row["leverage"],
            entry_signal_score=row["entry_signal_score"] or 0.0,
            entry_timescale=row["entry_timescale"] or "5m",
            entry_order_id=row["entry_order_id"],
            exit_order_id=row["exit_order_id"],
            realised_pnl=row["realised_pnl"] or 0.0,
            entry_commission=_safe_get(row, "entry_commission", 0.0) or 0.0,
            exit_commission=_safe_get(row, "exit_commission", 0.0) or 0.0,
            venue=_safe_get(row, "venue", "binance") or "binance",
            strategy_version=_safe_get(row, "strategy_version", "v1-composite")
            or "v1-composite",
            # ── PR B continuation state (all legacy-safe via _safe_get) ──
            hold_clock_anchor=hca_col.timestamp() if hca_col is not None else 0.0,
            continuation_count=int(_safe_get(row, "continuation_count", 0) or 0),
            last_continuation_ts=lct_col.timestamp() if lct_col is not None else 0.0,
            last_continuation_p_up=float(
                _safe_get(row, "last_continuation_p_up", 0.0) or 0.0
            ),
            # ── PR B v4 audit snapshot (all Optional, legacy rows → None) ──
            v4_entry_regime=_safe_get(row, "v4_entry_regime", None),
            v4_entry_macro_bias=_safe_get(row, "v4_entry_macro_bias", None),
            v4_entry_macro_confidence=_safe_get(row, "v4_entry_macro_confidence", None),
            v4_entry_expected_move_bps=_safe_get(row, "v4_entry_expected_move_bps", None),
            v4_entry_composite_v3=_safe_get(row, "v4_entry_composite_v3", None),
            v4_entry_consensus_safe=_safe_get(row, "v4_entry_consensus_safe", None),
            v4_entry_window_close_ts=_safe_get(row, "v4_entry_window_close_ts", None),
            v4_snapshot_ts_at_entry=_safe_get(row, "v4_snapshot_ts_at_entry", None),
        )
        if row["entry_price"]:
            p.entry_price = Price(value=row["entry_price"])
        if row["notional"]:
            p.notional = Money.usd(row["notional"])
        if row["collateral"]:
            p.collateral = Money.usd(row["collateral"])
        if row["stop_loss_price"]:
            p.stop_loss = StopLevel(price=row["stop_loss_price"])
        if row["take_profit_price"]:
            p.take_profit = StopLevel(price=row["take_profit_price"])
        if row["exit_price"]:
            p.exit_price = Price(value=row["exit_price"])
        if row["exit_reason"]:
            p.exit_reason = ExitReason(row["exit_reason"])
        if row["opened_at"]:
            p.opened_at = row["opened_at"].timestamp()
        if row["closed_at"]:
            p.closed_at = row["closed_at"].timestamp()
        return p

    # ── Read-side projections for the Trade Timeline tab ─────────────────
    # These return plain dicts rather than full Position entities because
    # the timeline UI needs a flattened shape with hold_duration_s computed
    # server-side and the legacy-default coalescing baked in. Keeping the
    # domain entity out of the HTTP-layer projection is a deliberate Clean
    # Arch choice: the inner layers never depend on the outer ones.

    async def get_closed_history(
        self,
        limit: int = 25,
        offset: int = 0,
        side: Optional[str] = None,
        outcome: Optional[str] = None,
        exit_reason: Optional[str] = None,
    ) -> list[dict]:
        """
        Paginated list of closed positions for the Trade Timeline dashboard.

        Filters:
          side       — "LONG" | "SHORT" | None
          outcome    — "win" | "loss" | None (uses realised_pnl sign)
          exit_reason — comma-separated list of exit reasons, or None

        Returns newest-first by closed_at.
        """
        query = """
            SELECT
                id, asset, side, state, leverage,
                entry_price, notional, collateral,
                stop_loss_price, take_profit_price,
                exit_price, exit_reason, realised_pnl,
                opened_at, closed_at,
                entry_signal_score, entry_timescale,
                entry_order_id, exit_order_id,
                COALESCE(entry_commission, 0) AS entry_commission,
                COALESCE(exit_commission, 0) AS exit_commission,
                COALESCE(venue, 'binance') AS venue,
                COALESCE(strategy_version, 'v1-composite') AS strategy_version,
                -- ── PR B: continuation state (legacy rows → 0 / NULL) ──
                COALESCE(continuation_count, 0) AS continuation_count,
                last_continuation_ts,
                last_continuation_p_up,
                -- ── PR B: v4 audit snapshot (all nullable, legacy rows → NULL) ──
                v4_entry_regime,
                v4_entry_macro_bias,
                v4_entry_macro_confidence,
                v4_entry_expected_move_bps,
                v4_entry_composite_v3,
                v4_entry_consensus_safe,
                v4_entry_window_close_ts,
                v4_snapshot_ts_at_entry,
                EXTRACT(EPOCH FROM (closed_at - opened_at)) AS hold_duration_s
            FROM margin_positions
            WHERE state = 'CLOSED'
              AND ($1::text IS NULL OR side = $1)
              AND ($2::text IS NULL OR (
                  ($2 = 'win' AND realised_pnl > 0)
                  OR ($2 = 'loss' AND realised_pnl <= 0)
              ))
              AND ($3::text IS NULL OR exit_reason = ANY(string_to_array($3, ',')))
            ORDER BY closed_at DESC NULLS LAST
            LIMIT $4 OFFSET $5
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, side, outcome, exit_reason, limit, offset)

        return [
            {
                "id": r["id"],
                "asset": r["asset"],
                "side": r["side"],
                "state": r["state"],
                "leverage": r["leverage"],
                "entry_price": r["entry_price"],
                "notional": r["notional"],
                "collateral": r["collateral"],
                "stop_loss_price": r["stop_loss_price"],
                "take_profit_price": r["take_profit_price"],
                "exit_price": r["exit_price"],
                "exit_reason": r["exit_reason"],
                "realised_pnl": r["realised_pnl"] or 0.0,
                "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
                "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
                "hold_duration_s": float(r["hold_duration_s"])
                if r["hold_duration_s"] is not None
                else None,
                "entry_signal_score": r["entry_signal_score"],
                "entry_timescale": r["entry_timescale"],
                "entry_order_id": r["entry_order_id"],
                "exit_order_id": r["exit_order_id"],
                "entry_commission": float(r["entry_commission"] or 0.0),
                "exit_commission": float(r["exit_commission"] or 0.0),
                "total_commission": float(
                    (r["entry_commission"] or 0.0) + (r["exit_commission"] or 0.0)
                ),
                "venue": r["venue"],
                "strategy_version": r["strategy_version"],
                # ── PR B: continuation state ──
                "continuation_count": int(r["continuation_count"] or 0),
                "last_continuation_ts": r["last_continuation_ts"].isoformat()
                if r["last_continuation_ts"] else None,
                "last_continuation_p_up": float(r["last_continuation_p_up"])
                if r["last_continuation_p_up"] is not None else None,
                # ── PR B: v4 audit snapshot ──
                "v4_entry_regime": r["v4_entry_regime"],
                "v4_entry_macro_bias": r["v4_entry_macro_bias"],
                "v4_entry_macro_confidence": int(r["v4_entry_macro_confidence"])
                if r["v4_entry_macro_confidence"] is not None else None,
                "v4_entry_expected_move_bps": float(r["v4_entry_expected_move_bps"])
                if r["v4_entry_expected_move_bps"] is not None else None,
                "v4_entry_composite_v3": float(r["v4_entry_composite_v3"])
                if r["v4_entry_composite_v3"] is not None else None,
                "v4_entry_consensus_safe": r["v4_entry_consensus_safe"],
                "v4_entry_window_close_ts": int(r["v4_entry_window_close_ts"])
                if r["v4_entry_window_close_ts"] is not None else None,
                "v4_snapshot_ts_at_entry": float(r["v4_snapshot_ts_at_entry"])
                if r["v4_snapshot_ts_at_entry"] is not None else None,
            }
            for r in rows
        ]

    async def get_closed_history_count(
        self,
        side: Optional[str] = None,
        outcome: Optional[str] = None,
        exit_reason: Optional[str] = None,
    ) -> int:
        """
        Count of closed positions matching the same filters as get_closed_history.
        Used for pagination UI — lets the frontend show "Page X of Y".
        """
        query = """
            SELECT COUNT(*) AS total FROM margin_positions
            WHERE state = 'CLOSED'
              AND ($1::text IS NULL OR side = $1)
              AND ($2::text IS NULL OR (
                  ($2 = 'win' AND realised_pnl > 0)
                  OR ($2 = 'loss' AND realised_pnl <= 0)
              ))
              AND ($3::text IS NULL OR exit_reason = ANY(string_to_array($3, ',')))
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, side, outcome, exit_reason)
        return int(row["total"]) if row else 0


def _ts(epoch: float) -> Optional[datetime]:
    if epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _safe_get(row, key: str, default):
    """
    asyncpg Record raises KeyError on missing columns (unlike dict.get).

    Use this when reading columns added by additive migrations — if the row
    came from a SELECT * that ran against a schema without the column yet,
    we fall back gracefully. Belt-and-braces: in practice ensure_table
    should have run the ALTERs before any SELECT reaches this path, but
    defensive code here is cheap and prevents boot-time surprises.
    """
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return value if value is not None else default
