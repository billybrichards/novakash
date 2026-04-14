"""PostgreSQL Trade Repository -- per-aggregate persistence for trade data.

Handles all trade CRUD, manual trade queue operations, and POLY-SOT
reconciliation columns for both the ``trades`` and ``manual_trades`` tables.

Delegates to the **exact same SQL** that ``engine/persistence/db_client.py``
uses today.  This is a thin structural split -- zero behaviour change.

Phase 2 will wire this into the composition root.  Until then, nothing
imports this module so there is zero runtime risk.

Audit: CA-01 (Clean Architecture migration -- split god-class DBClient).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
import structlog

log = structlog.get_logger(__name__)


class PgTradeRepository:
    """asyncpg-backed trade repository.

    Accepts an ``asyncpg.Pool`` -- the same pool the legacy ``DBClient``
    uses.  Methods copy SQL verbatim from ``db_client.py`` so behaviour
    parity is byte-for-byte.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # -- Trade Writes ------------------------------------------------------

    async def write_trade(self, order) -> None:
        """Persist a resolved or open trade to the ``trades`` table.

        Verbatim SQL from ``DBClient.write_trade``.

        Args:
            order: The fully populated Order dataclass
                   (``execution.order_manager.Order``).
        """
        if not self._pool:
            return

        query = """
            INSERT INTO trades (
                order_id, strategy, venue, market_slug, direction,
                entry_price, stake_usd, fee_usd, status, outcome,
                payout_usd, pnl_usd, created_at, resolved_at, metadata, mode,
                is_live,
                engine_version, clob_order_id, fill_price, fill_size, execution_mode
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                      $18,$19,$20,$21,$22)
            ON CONFLICT (order_id) DO UPDATE SET
                status         = EXCLUDED.status,
                outcome        = EXCLUDED.outcome,
                payout_usd     = EXCLUDED.payout_usd,
                pnl_usd        = EXCLUDED.pnl_usd,
                resolved_at    = EXCLUDED.resolved_at,
                is_live        = EXCLUDED.is_live,
                entry_price    = EXCLUDED.entry_price,
                stake_usd      = EXCLUDED.stake_usd,
                metadata       = EXCLUDED.metadata,
                clob_order_id  = COALESCE(EXCLUDED.clob_order_id, trades.clob_order_id),
                fill_price     = COALESCE(EXCLUDED.fill_price, trades.fill_price),
                fill_size      = COALESCE(EXCLUDED.fill_size, trades.fill_size),
                execution_mode = COALESCE(EXCLUDED.execution_mode, trades.execution_mode)
        """

        try:
            # Convert unix float timestamps -> datetime for TIMESTAMPTZ columns
            created_dt = (
                datetime.fromtimestamp(order.created_at, tz=timezone.utc)
                if isinstance(order.created_at, (int, float))
                else order.created_at
            )
            resolved_dt = (
                datetime.fromtimestamp(order.resolved_at, tz=timezone.utc)
                if isinstance(order.resolved_at, (int, float))
                else order.resolved_at
            )

            # Extract v8.0 fields from metadata if present
            meta = order.metadata or {}
            clob_order_id = meta.get("clob_order_id") or meta.get("order_id")
            fill_price = meta.get("fill_price") or meta.get("avg_price")
            fill_size = meta.get("fill_size") or meta.get("size_matched")
            execution_mode = meta.get("execution_mode", "paper")

            async with self._pool.acquire() as conn:
                await conn.execute(
                    query,
                    order.order_id,
                    order.strategy,
                    order.venue,
                    order.market_slug,
                    order.direction,
                    float(order.entry_price),
                    order.stake_usd,
                    order.fee_usd,
                    order.status.value,
                    order.outcome,
                    order.payout_usd,
                    order.pnl_usd,
                    created_dt,
                    resolved_dt,
                    json.dumps(order.metadata),
                    "paper" if execution_mode == "paper" else "live",
                    execution_mode != "paper",
                    # v8.0 fields
                    "v8.0",
                    clob_order_id,
                    float(fill_price) if fill_price is not None else None,
                    float(fill_size) if fill_size is not None else None,
                    execution_mode,
                )
            log.debug("db.trade_written", order_id=order.order_id)
        except Exception as exc:
            log.error("db.write_trade_failed", order_id=order.order_id, error=str(exc))
            raise

    # Alias for backward compat
    async def save_trade(self, order) -> None:
        """Alias for write_trade (used by OrderManager)."""
        await self.write_trade(order)

    # -- Window Dedup Queries -----------------------------------------------

    async def load_recent_traded_windows(self, hours: int = 2) -> set[str]:
        """Load recently traded window keys from the trades table.

        Verbatim SQL from ``DBClient.load_recent_traded_windows``.
        """
        if not self._pool:
            return set()

        query = """
            SELECT DISTINCT
                COALESCE(metadata->>'asset', 'BTC') AS asset,
                metadata->>'window_ts' AS wts
            FROM trades
            WHERE created_at > NOW() - make_interval(hours => $1)
              AND strategy = 'five_min_vpin'
        """
        traded: set[str] = set()
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, hours)
                for r in rows:
                    wts = r["wts"]
                    asset = r["asset"] or "BTC"
                    if wts:
                        traded.add(f"{asset}-{wts}")
            log.info("db.loaded_traded_windows", count=len(traded), hours=hours)
        except Exception as exc:
            log.error("db.load_traded_windows_failed", error=str(exc))
        return traded

    # -- Read Helpers -------------------------------------------------------

    async def get_daily_pnl(self, date: Optional[datetime] = None) -> float:
        """Return total realised PnL for the given date (default: today).

        Verbatim SQL from ``DBClient.get_daily_pnl``.
        """
        if not self._pool:
            return 0.0

        target = date or datetime.utcnow()
        query = """
            SELECT COALESCE(SUM(pnl_usd), 0)
            FROM trades
            WHERE DATE(resolved_at) = $1::date
              AND pnl_usd IS NOT NULL
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchval(query, target)
        return float(row or 0)

    async def get_open_trades(self, hours_back: int = 24) -> list[dict]:
        """Get OPEN trades from the last N hours for recovery on startup.

        Verbatim SQL from ``DBClient.get_open_trades``.
        """
        if not self._pool:
            return []
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT order_id, direction, entry_price, stake_usd,
                           metadata, created_at, strategy, venue, market_slug, fee_usd
                    FROM trades
                    WHERE status = 'OPEN'
                      AND created_at > $1
                    ORDER BY created_at ASC
                    """,
                    cutoff,
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.error("db.get_open_trades_failed", error=str(exc))
            return []

    async def find_unresolved_paper_trades(
        self, min_age_seconds: int = 360
    ) -> list[dict]:
        """Return OPEN paper trades old enough for their window to have resolved.

        Implements TradeRepository.find_unresolved_paper_trades.
        ``window_ts`` is extracted from ``metadata->>'window_ts'`` and returned
        as a string — callers cast to int.
        """
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT id, order_id, direction, stake_usd, entry_price,
                              execution_mode, metadata, strategy,
                              COALESCE(metadata->>'asset', 'BTC') AS asset,
                              COALESCE(metadata->>'window_ts',
                                SUBSTRING(market_slug FROM '[0-9]+$')) AS window_ts,
                              created_at
                       FROM trades
                       WHERE order_id LIKE 'paper-%'
                         AND outcome IS NULL
                         AND status IN ('OPEN', 'EXPIRED')
                         AND created_at < NOW() - ($1::int * INTERVAL '1 second')
                       ORDER BY created_at ASC""",
                    min_age_seconds,
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.warning(
                "pg_trade_repo.find_unresolved_paper_failed",
                error=str(exc)[:100],
            )
            return []

    async def resolve_trade(
        self,
        trade_id: str,
        outcome: str,
        pnl_usd: float,
        status: str,
    ) -> None:
        """UPDATE trades SET outcome, pnl_usd, resolved_at, status WHERE id."""
        if not self._pool:
            return
        try:
            from datetime import datetime, timezone
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """UPDATE trades
                       SET outcome = $1, pnl_usd = $2, status = $3,
                           resolved_at = $4
                       WHERE id = $5 AND outcome IS NULL""",
                    outcome,
                    pnl_usd,
                    status,
                    datetime.now(timezone.utc),
                    int(trade_id),
                )
        except Exception as exc:
            log.warning(
                "pg_trade_repo.resolve_trade_failed",
                trade_id=trade_id,
                error=str(exc)[:100],
            )

    async def mark_trade_expired(self, order_id: str) -> None:
        """Mark a trade as EXPIRED in the DB (used by startup reconciliation).

        Verbatim SQL from ``DBClient.mark_trade_expired``.
        Safety: refuses to expire trades with confirmed CLOB fills.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                # Guard: do not expire trades with confirmed fills
                row = await conn.fetchrow(
                    """SELECT metadata->>'clob_status' as clob_status,
                              metadata->>'shares_filled' as shares_filled
                       FROM trades WHERE order_id = $1""",
                    order_id,
                )
                if row:
                    clob_status = (row["clob_status"] or "").upper()
                    shares_filled = float(row["shares_filled"] or 0)
                    if clob_status == "MATCHED" or shares_filled > 0:
                        log.info(
                            "db.trade_expire_blocked_has_fill",
                            order_id=order_id[:24] if len(order_id) > 24 else order_id,
                            clob_status=clob_status,
                            shares_filled=shares_filled,
                        )
                        return

                await conn.execute(
                    "UPDATE trades SET status = 'EXPIRED', resolved_at = $1 WHERE order_id = $2",
                    datetime.now(timezone.utc),
                    order_id,
                )
            log.info(
                "db.trade_marked_expired",
                order_id=order_id[:24] if len(order_id) > 24 else order_id,
            )
        except Exception as exc:
            log.error("db.mark_trade_expired_failed", order_id=order_id, error=str(exc))

    async def ensure_v8_trade_columns(self) -> None:
        """Add v8.0 columns to trades table if missing (idempotent).

        Verbatim SQL from ``DBClient.ensure_v8_trade_columns``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                for col, col_type in [
                    ("engine_version", "VARCHAR(10)"),
                    ("clob_order_id", "VARCHAR(128)"),
                    ("fill_price", "DOUBLE PRECISION"),
                    ("fill_size", "DOUBLE PRECISION"),
                    ("execution_mode", "VARCHAR(20)"),
                    ("is_live", "BOOLEAN DEFAULT FALSE"),
                ]:
                    await conn.execute(
                        f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    )
            log.info("db.v8_trade_columns_ensured")
        except Exception as exc:
            log.warning("db.ensure_v8_trade_columns_failed", error=str(exc))

    # -- Manual Trade Queue (v5.8 Dashboard) --------------------------------

    async def poll_pending_live_trades(self) -> list:
        """Fetch manual trades with status='pending_live' for engine execution.

        Verbatim SQL from ``DBClient.poll_pending_live_trades``.
        """
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT trade_id, window_ts, asset, direction, entry_price,
                           gamma_up_price, gamma_down_price, stake_usd
                    FROM manual_trades
                    WHERE status = 'pending_live'
                    ORDER BY created_at ASC
                    LIMIT 5
                """)
                return [dict(r) for r in rows]
        except Exception as exc:
            log.debug("db.poll_pending_live_failed", error=str(exc))
            return []

    async def get_token_ids_from_market_data(
        self,
        asset: str,
        window_ts: int,
        timeframe: str = "5m",
    ) -> dict | None:
        """LT-02: fallback lookup for CLOB token_ids.

        Verbatim SQL from ``DBClient.get_token_ids_from_market_data``.
        """
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT up_token_id, down_token_id
                    FROM market_data
                    WHERE asset = $1
                      AND timeframe = $2
                      AND window_ts BETWEEN ($3::bigint - 60) AND ($3::bigint + 60)
                      AND up_token_id IS NOT NULL
                      AND down_token_id IS NOT NULL
                    ORDER BY ABS(window_ts - $3::bigint) ASC
                    LIMIT 1
                """,
                    asset,
                    timeframe,
                    int(window_ts),
                )
                if row and row["up_token_id"] and row["down_token_id"]:
                    return {
                        "up_token_id": row["up_token_id"],
                        "down_token_id": row["down_token_id"],
                    }
                return None
        except Exception as exc:
            log.debug("db.get_token_ids_from_market_data_failed", error=str(exc)[:200])
            return None

    async def update_manual_trade_status(
        self,
        trade_id: str,
        status: str,
        pnl_usd: float = None,
        outcome_direction: str = None,
        clob_order_id: str = None,
    ) -> None:
        """Update a manual trade after execution or resolution.

        Verbatim SQL from ``DBClient.update_manual_trade_status``.
        POLY-SOT: also persists clob_order_id to polymarket_order_id column.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                if pnl_usd is not None:
                    await conn.execute(
                        """
                        UPDATE manual_trades
                        SET status = $1, pnl_usd = $2, outcome_direction = $3, resolved_at = NOW()
                        WHERE trade_id = $4
                    """,
                        status,
                        pnl_usd,
                        outcome_direction,
                        trade_id,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE manual_trades SET status = $1 WHERE trade_id = $2
                    """,
                        status,
                        trade_id,
                    )
                # POLY-SOT: persist the CLOB order ID separately if provided.
                if clob_order_id:
                    try:
                        await conn.execute(
                            """
                            UPDATE manual_trades
                            SET polymarket_order_id = $1
                            WHERE trade_id = $2
                              AND (polymarket_order_id IS NULL OR polymarket_order_id = '')
                        """,
                            clob_order_id,
                            trade_id,
                        )
                    except Exception as col_exc:
                        log.warning(
                            "db.manual_trade_polymarket_order_id_update_failed",
                            trade_id=trade_id,
                            error=str(col_exc)[:120],
                        )
            log.info(
                "db.manual_trade_updated",
                trade_id=trade_id,
                status=status,
                clob_order_id=clob_order_id[:20] if clob_order_id else None,
            )
        except Exception as exc:
            log.error("db.manual_trade_update_failed", error=str(exc))

    # -- POLY-SOT helpers (manual_trades table) -----------------------------

    async def ensure_manual_trades_sot_columns(self) -> None:
        """Add POLY-SOT columns to manual_trades if missing (idempotent).

        Verbatim SQL from ``DBClient.ensure_manual_trades_sot_columns``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                for col, col_type in [
                    ("polymarket_order_id", "TEXT"),
                    ("polymarket_confirmed_status", "TEXT"),
                    ("polymarket_confirmed_fill_price", "NUMERIC(18,6)"),
                    ("polymarket_confirmed_size", "NUMERIC(18,6)"),
                    ("polymarket_confirmed_at", "TIMESTAMPTZ"),
                    ("polymarket_last_verified_at", "TIMESTAMPTZ"),
                    ("sot_reconciliation_state", "TEXT"),
                    ("sot_reconciliation_notes", "TEXT"),
                    ("polymarket_tx_hash", "TEXT"),
                ]:
                    await conn.execute(
                        f"ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_manual_trades_polymarket_order_id "
                    "ON manual_trades(polymarket_order_id) WHERE polymarket_order_id IS NOT NULL"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_manual_trades_sot_state "
                    "ON manual_trades(sot_reconciliation_state) WHERE sot_reconciliation_state IS NOT NULL"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_manual_trades_tx_hash "
                    "ON manual_trades(polymarket_tx_hash) WHERE polymarket_tx_hash IS NOT NULL"
                )
            log.info("db.manual_trades_sot_columns_ensured")
        except Exception as exc:
            log.warning("db.ensure_manual_trades_sot_columns_failed", error=str(exc))

    async def fetch_manual_trades_for_sot_check(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return recent manual_trades rows for SOT reconciliation.

        Verbatim SQL from ``DBClient.fetch_manual_trades_for_sot_check``.
        """
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        trade_id,
                        polymarket_order_id,
                        status,
                        mode,
                        direction,
                        entry_price,
                        stake_usd,
                        created_at,
                        polymarket_confirmed_status,
                        polymarket_confirmed_fill_price,
                        polymarket_confirmed_size,
                        polymarket_confirmed_at,
                        polymarket_last_verified_at,
                        sot_reconciliation_state,
                        sot_reconciliation_notes
                    FROM manual_trades
                    WHERE created_at < NOW() - INTERVAL '30 seconds'
                      AND ($1::timestamptz IS NULL OR created_at >= $1)
                      AND status IN (
                          'executed', 'executing', 'open',
                          'pending_live', 'pending_paper', 'live'
                      )
                      AND (
                          sot_reconciliation_state IS NULL
                          OR sot_reconciliation_state IN ('unreconciled', 'engine_optimistic', 'diverged')
                          OR polymarket_last_verified_at IS NULL
                          OR polymarket_last_verified_at < NOW() - INTERVAL '5 minutes'
                      )
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    since,
                    int(limit),
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.warning(
                "db.fetch_manual_trades_for_sot_check_failed", error=str(exc)[:200]
            )
            return []

    async def update_manual_trade_sot(
        self,
        trade_id: str,
        *,
        polymarket_confirmed_status: Optional[str],
        polymarket_confirmed_fill_price: Optional[float],
        polymarket_confirmed_size: Optional[float],
        polymarket_confirmed_at: Optional[datetime],
        sot_reconciliation_state: str,
        sot_reconciliation_notes: Optional[str],
    ) -> None:
        """Stamp a manual_trades row with SOT reconciliation result.

        Verbatim SQL from ``DBClient.update_manual_trade_sot``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE manual_trades
                    SET polymarket_confirmed_status = $1,
                        polymarket_confirmed_fill_price = $2,
                        polymarket_confirmed_size = $3,
                        polymarket_confirmed_at = $4,
                        polymarket_last_verified_at = NOW(),
                        sot_reconciliation_state = $5,
                        sot_reconciliation_notes = $6
                    WHERE trade_id = $7
                    """,
                    polymarket_confirmed_status,
                    polymarket_confirmed_fill_price,
                    polymarket_confirmed_size,
                    polymarket_confirmed_at,
                    sot_reconciliation_state,
                    sot_reconciliation_notes,
                    trade_id,
                )
            log.info(
                "db.manual_trade_sot_updated",
                trade_id=trade_id,
                state=sot_reconciliation_state,
                confirmed_status=polymarket_confirmed_status,
            )
        except Exception as exc:
            log.warning(
                "db.update_manual_trade_sot_failed",
                trade_id=trade_id,
                error=str(exc)[:200],
            )

    # -- POLY-SOT-b helpers (trades table) ----------------------------------

    async def ensure_trades_sot_columns(self) -> None:
        """Add POLY-SOT columns to the ``trades`` table if missing (idempotent).

        Verbatim SQL from ``DBClient.ensure_trades_sot_columns``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                for col, col_type in [
                    ("polymarket_order_id", "TEXT"),
                    ("polymarket_confirmed_status", "TEXT"),
                    ("polymarket_confirmed_fill_price", "NUMERIC(18,6)"),
                    ("polymarket_confirmed_size", "NUMERIC(18,6)"),
                    ("polymarket_confirmed_at", "TIMESTAMPTZ"),
                    ("polymarket_last_verified_at", "TIMESTAMPTZ"),
                    ("sot_reconciliation_state", "TEXT"),
                    ("sot_reconciliation_notes", "TEXT"),
                    ("polymarket_tx_hash", "TEXT"),
                ]:
                    await conn.execute(
                        f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trades_polymarket_order_id "
                    "ON trades(polymarket_order_id) WHERE polymarket_order_id IS NOT NULL"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trades_sot_state "
                    "ON trades(sot_reconciliation_state) WHERE sot_reconciliation_state IS NOT NULL"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trades_tx_hash "
                    "ON trades(polymarket_tx_hash) WHERE polymarket_tx_hash IS NOT NULL"
                )
            log.info("db.trades_sot_columns_ensured")
        except Exception as exc:
            log.warning("db.ensure_trades_sot_columns_failed", error=str(exc))

    async def fetch_trades_for_sot_check(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return recent automatic-trade rows for SOT reconciliation.

        Verbatim SQL from ``DBClient.fetch_trades_for_sot_check``.
        """
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        id AS trade_id,
                        order_id,
                        COALESCE(polymarket_order_id, clob_order_id) AS polymarket_order_id,
                        status,
                        mode,
                        direction,
                        entry_price,
                        stake_usd,
                        fill_price,
                        fill_size,
                        created_at,
                        is_live,
                        polymarket_confirmed_status,
                        polymarket_confirmed_fill_price,
                        polymarket_confirmed_size,
                        polymarket_confirmed_at,
                        polymarket_last_verified_at,
                        sot_reconciliation_state,
                        sot_reconciliation_notes
                    FROM trades
                    WHERE created_at < NOW() - INTERVAL '30 seconds'
                      AND ($1::timestamptz IS NULL OR created_at >= $1)
                      AND COALESCE(is_live, FALSE) = TRUE
                      AND status IN (
                          'FILLED', 'OPEN', 'PENDING', 'MATCHED',
                          'filled', 'open', 'pending', 'matched',
                          'EXPIRED', 'expired'
                      )
                      AND (
                          sot_reconciliation_state IS NULL
                          OR sot_reconciliation_state IN ('unreconciled', 'engine_optimistic', 'diverged')
                          OR polymarket_last_verified_at IS NULL
                          OR polymarket_last_verified_at < NOW() - INTERVAL '5 minutes'
                      )
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    since,
                    int(limit),
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("db.fetch_trades_for_sot_check_failed", error=str(exc)[:200])
            return []

    async def update_trade_sot(
        self,
        trade_id,
        *,
        polymarket_confirmed_status: Optional[str],
        polymarket_confirmed_fill_price: Optional[float],
        polymarket_confirmed_size: Optional[float],
        polymarket_confirmed_at: Optional[datetime],
        sot_reconciliation_state: str,
        sot_reconciliation_notes: Optional[str],
    ) -> None:
        """Stamp a ``trades`` row with SOT reconciliation result.

        Verbatim SQL from ``DBClient.update_trade_sot``.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE trades
                    SET polymarket_confirmed_status = $1,
                        polymarket_confirmed_fill_price = $2,
                        polymarket_confirmed_size = $3,
                        polymarket_confirmed_at = $4,
                        polymarket_last_verified_at = NOW(),
                        sot_reconciliation_state = $5,
                        sot_reconciliation_notes = $6
                    WHERE id = $7
                    """,
                    polymarket_confirmed_status,
                    polymarket_confirmed_fill_price,
                    polymarket_confirmed_size,
                    polymarket_confirmed_at,
                    sot_reconciliation_state,
                    sot_reconciliation_notes,
                    int(trade_id),
                )
            log.info(
                "db.trade_sot_updated",
                trade_id=trade_id,
                state=sot_reconciliation_state,
                confirmed_status=polymarket_confirmed_status,
            )
        except Exception as exc:
            log.warning(
                "db.update_trade_sot_failed",
                trade_id=trade_id,
                error=str(exc)[:200],
            )
