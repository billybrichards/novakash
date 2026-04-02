"""
Database Client — Async PostgreSQL writes via asyncpg.

Handles all persistence for the trading engine:
  - Trade records (placed, resolved, PnL)
  - Signal snapshots (VPIN, cascade, arb)
  - System state (heartbeat, kill-switch status, bankroll, feed connectivity)

Schema reference: hub/db/schema.sql
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
        # Use lowercase field from settings (pydantic model)
                # Strip SQLAlchemy dialect prefix if present (asyncpg needs plain postgresql://)
        dsn = settings.database_url
        if dsn.startswith("postgresql+asyncpg://"):
            dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Open the asyncpg connection pool."""
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        log.info("db.connected")

    async def close(self) -> None:
        """Close all pooled connections."""
        if self._pool:
            await self._pool.close()
            log.info("db.closed")

    def _assert_pool(self) -> None:
        assert self._pool, "DBClient not connected — call connect() first"

    # ─── Trade Writes ─────────────────────────────────────────────────────────

    async def write_trade(self, order: Order) -> None:
        """
        Persist a resolved or open trade to the `trades` table.

        Args:
            order: The fully populated Order dataclass.
        """
        self._assert_pool()

        query = """
            INSERT INTO trades (
                order_id, strategy, venue, market_slug, direction,
                entry_price, stake_usd, fee_usd, status, outcome,
                payout_usd, pnl_usd, created_at, resolved_at, metadata, mode
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            ON CONFLICT (order_id) DO UPDATE SET
                status      = EXCLUDED.status,
                outcome     = EXCLUDED.outcome,
                payout_usd  = EXCLUDED.payout_usd,
                pnl_usd     = EXCLUDED.pnl_usd,
                resolved_at = EXCLUDED.resolved_at
        """

        try:
            # Convert unix float timestamps → datetime for TIMESTAMPTZ columns
            from datetime import datetime, timezone
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
                    "live" if order.order_id.startswith("0x") else "paper",
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
        value: float,
        metadata: Optional[dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """
        Persist a signal snapshot to the `signals` table.

        Schema: signals(id, signal_type, value DECIMAL, metadata JSONB, created_at)

        Args:
            signal_type: "vpin" | "cascade" | "arb" | "regime"
            value:       Primary numeric value for the signal (e.g. VPIN score).
            metadata:    Additional signal data as a dict (stored as JSONB).
            timestamp:   Signal timestamp; defaults to now.
        """
        self._assert_pool()

        ts = timestamp or datetime.utcnow()
        query = """
            INSERT INTO signals (signal_type, value, metadata, created_at)
            VALUES ($1, $2, $3::jsonb, $4)
        """

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    query,
                    signal_type,
                    float(value),
                    json.dumps(metadata or {}),
                    ts,
                )
            log.debug("db.signal_written", type=signal_type, value=value)
        except Exception as exc:
            log.error("db.write_signal_failed", type=signal_type, error=str(exc))
            raise

    # ─── System State ─────────────────────────────────────────────────────────

    async def update_system_state(
        self,
        engine_status: str = "running",
        current_balance: Optional[float] = None,
        peak_balance: Optional[float] = None,
        current_drawdown_pct: Optional[float] = None,
        last_vpin: Optional[float] = None,
        last_cascade_state: Optional[str] = None,
        active_positions: int = 0,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Upsert the engine's current system state (single-row singleton record).

        Maps directly to the `system_state` table columns.
        """
        self._assert_pool()

        query = """
            INSERT INTO system_state (
                id, engine_status, current_balance, peak_balance,
                current_drawdown_pct, last_vpin, last_cascade_state,
                active_positions, last_heartbeat, config
            )
            VALUES (1, $1, $2, $3, $4, $5, $6, $7, NOW(), $8::jsonb)
            ON CONFLICT (id) DO UPDATE SET
                engine_status        = EXCLUDED.engine_status,
                current_balance      = COALESCE(EXCLUDED.current_balance, system_state.current_balance),
                peak_balance         = COALESCE(EXCLUDED.peak_balance, system_state.peak_balance),
                current_drawdown_pct = COALESCE(EXCLUDED.current_drawdown_pct, system_state.current_drawdown_pct),
                last_vpin            = COALESCE(EXCLUDED.last_vpin, system_state.last_vpin),
                last_cascade_state   = COALESCE(EXCLUDED.last_cascade_state, system_state.last_cascade_state),
                active_positions     = EXCLUDED.active_positions,
                last_heartbeat       = NOW(),
                config               = COALESCE(EXCLUDED.config, system_state.config)
        """

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    query,
                    engine_status,
                    current_balance,
                    peak_balance,
                    current_drawdown_pct,
                    last_vpin,
                    last_cascade_state,
                    active_positions,
                    json.dumps(config or {}),
                )
        except Exception as exc:
            log.error("db.update_system_state_failed", error=str(exc))
            raise

    async def update_heartbeat(self) -> None:
        """
        Update last_heartbeat to NOW() without touching other fields.

        Called every 10 seconds by the orchestrator heartbeat loop.
        """
        self._assert_pool()

        query = """
            INSERT INTO system_state (id, engine_status, last_heartbeat)
            VALUES (1, 'running', NOW())
            ON CONFLICT (id) DO UPDATE SET last_heartbeat = NOW()
        """

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(query)
        except Exception as exc:
            log.error("db.update_heartbeat_failed", error=str(exc))
            # Don't re-raise — heartbeat failure is not fatal

    async def update_feed_status(
        self,
        binance: Optional[bool] = None,
        coinglass: Optional[bool] = None,
        chainlink: Optional[bool] = None,
        polymarket: Optional[bool] = None,
        opinion: Optional[bool] = None,
    ) -> None:
        """
        Update feed connection status boolean flags in system_state.

        Only updates columns that are explicitly passed (not None).
        """
        self._assert_pool()

        # Build dynamic SET clause for non-None values
        updates: list[str] = []
        params: list[Any] = []
        param_idx = 1

        if binance is not None:
            updates.append(f"binance_connected = ${param_idx}")
            params.append(binance)
            param_idx += 1
        if coinglass is not None:
            updates.append(f"coinglass_connected = ${param_idx}")
            params.append(coinglass)
            param_idx += 1
        if chainlink is not None:
            updates.append(f"chainlink_connected = ${param_idx}")
            params.append(chainlink)
            param_idx += 1
        if polymarket is not None:
            updates.append(f"polymarket_connected = ${param_idx}")
            params.append(polymarket)
            param_idx += 1
        if opinion is not None:
            updates.append(f"opinion_connected = ${param_idx}")
            params.append(opinion)
            param_idx += 1

        if not updates:
            return

        set_clause = ", ".join(updates)
        query = f"""
            INSERT INTO system_state (id, engine_status, {', '.join(
                c.split(' = ')[0] for c in updates
            )})
            VALUES (1, 'running', {', '.join(f'${i+1}' for i in range(len(params)))})
            ON CONFLICT (id) DO UPDATE SET {set_clause}
        """

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(query, *params)
            log.debug("db.feed_status_updated")
        except Exception as exc:
            log.error("db.update_feed_status_failed", error=str(exc))
            # Don't re-raise — status update failure is not fatal

    # ─── Read Helpers ─────────────────────────────────────────────────────────

    async def get_daily_pnl(self, date: Optional[datetime] = None) -> float:
        """Return total realised PnL for the given date (default: today)."""
        self._assert_pool()

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
