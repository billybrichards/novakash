"""
Database Client — Async PostgreSQL writes via asyncpg.

Handles all persistence for the trading engine:
  - Trade records (placed, resolved, PnL)
  - Signal snapshots (VPIN, cascade, arb)
  - System state (heartbeat, kill-switch status, bankroll)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional
import asyncpg
import structlog

from config.settings import Settings
from execution.order_manager import Order

log = structlog.get_logger(__name__)


class DBClient:
    """
    Thin async wrapper around asyncpg for writing trading data to PostgreSQL.

    Manages a connection pool; call `connect()` before use and `close()` on shutdown.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.DATABASE_URL
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Open the asyncpg connection pool."""
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        log.info("db.connected", dsn=self._dsn)

    async def close(self) -> None:
        """Close all pooled connections."""
        if self._pool:
            await self._pool.close()
            log.info("db.closed")

    # ─── Trade Writes ─────────────────────────────────────────────────────────

    async def write_trade(self, order: Order) -> None:
        """
        Persist a resolved or open trade to the `trades` table.

        Args:
            order: The fully populated Order dataclass.
        """
        assert self._pool, "DBClient not connected — call connect() first"

        query = """
            INSERT INTO trades (
                order_id, strategy, venue, market_slug, direction,
                entry_price, stake_usd, fee_usd, status, outcome,
                payout_usd, pnl_usd, created_at, resolved_at, metadata
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            ON CONFLICT (order_id) DO UPDATE SET
                status      = EXCLUDED.status,
                outcome     = EXCLUDED.outcome,
                payout_usd  = EXCLUDED.payout_usd,
                pnl_usd     = EXCLUDED.pnl_usd,
                resolved_at = EXCLUDED.resolved_at
        """

        try:
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
                    order.created_at,
                    order.resolved_at,
                    json.dumps(order.metadata),
                )
            log.debug("db.trade_written", order_id=order.order_id)
        except Exception as exc:
            log.error("db.write_trade_failed", order_id=order.order_id, error=str(exc))
            raise

    # Alias for backward compat
    async def save_trade(self, order: Order) -> None:
        """Alias for write_trade (used by OrderManager)."""
        await self.write_trade(order)

    # ─── Signal Writes ────────────────────────────────────────────────────────

    async def write_signal(
        self,
        signal_type: str,
        payload: dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> None:
        """
        Persist a signal snapshot to the `signals` table.

        Args:
            signal_type: "vpin" | "cascade" | "arb" | "regime"
            payload:     Signal data as a dict (stored as JSONB).
            timestamp:   Signal timestamp; defaults to now.
        """
        assert self._pool, "DBClient not connected"

        ts = timestamp or datetime.utcnow()
        query = """
            INSERT INTO signals (signal_type, payload, created_at)
            VALUES ($1, $2::jsonb, $3)
        """

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(query, signal_type, json.dumps(payload), ts)
            log.debug("db.signal_written", type=signal_type)
        except Exception as exc:
            log.error("db.write_signal_failed", type=signal_type, error=str(exc))
            raise

    # ─── System State ─────────────────────────────────────────────────────────

    async def update_system_state(self, state: dict[str, Any]) -> None:
        """
        Upsert the engine's current system state (single-row heartbeat record).

        Args:
            state: Key-value snapshot of engine health metrics.
        """
        assert self._pool, "DBClient not connected"

        query = """
            INSERT INTO system_state (id, state, updated_at)
            VALUES (1, $1::jsonb, NOW())
            ON CONFLICT (id) DO UPDATE SET
                state      = EXCLUDED.state,
                updated_at = EXCLUDED.updated_at
        """

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(query, json.dumps(state))
        except Exception as exc:
            log.error("db.update_system_state_failed", error=str(exc))
            raise

    # ─── Read Helpers ─────────────────────────────────────────────────────────

    async def get_daily_pnl(self, date: Optional[datetime] = None) -> float:
        """Return total realised PnL for the given date (default: today)."""
        assert self._pool, "DBClient not connected"

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
