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

    # ─── Playwright State ────────────────────────────────────────────────────

    async def ensure_playwright_tables(self) -> None:
        """Create playwright_state and redeem_events tables if they don't exist."""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS playwright_state (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    logged_in BOOLEAN DEFAULT FALSE,
                    browser_alive BOOLEAN DEFAULT FALSE,
                    usdc_balance DOUBLE PRECISION DEFAULT 0,
                    positions_value DOUBLE PRECISION DEFAULT 0,
                    positions_json JSONB DEFAULT '[]'::jsonb,
                    redeemable_json JSONB DEFAULT '[]'::jsonb,
                    history_json JSONB DEFAULT '[]'::jsonb,
                    screenshot_png BYTEA,
                    redeem_requested BOOLEAN DEFAULT FALSE,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                INSERT INTO playwright_state (id) VALUES (1) ON CONFLICT DO NOTHING;
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS redeem_events (
                    id SERIAL PRIMARY KEY,
                    redeemed_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    total_value DOUBLE PRECISION DEFAULT 0,
                    details_json JSONB DEFAULT '[]'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
        log.info("db.playwright_tables_ensured")

    async def update_playwright_state(
        self,
        logged_in: bool = False,
        browser_alive: bool = False,
        usdc_balance: float = 0.0,
        positions_value: float = 0.0,
        positions_json: Optional[list] = None,
        redeemable_json: Optional[list] = None,
        history_json: Optional[list] = None,
        screenshot_png: Optional[bytes] = None,
    ) -> None:
        """Upsert the playwright_state singleton row."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                if screenshot_png is not None:
                    await conn.execute(
                        """
                        UPDATE playwright_state SET
                            logged_in = $1, browser_alive = $2,
                            usdc_balance = $3, positions_value = $4,
                            positions_json = $5, redeemable_json = $6,
                            history_json = $7, screenshot_png = $8,
                            updated_at = NOW()
                        WHERE id = 1
                        """,
                        logged_in, browser_alive,
                        usdc_balance, positions_value,
                        json.dumps(positions_json or []),
                        json.dumps(redeemable_json or []),
                        json.dumps(history_json or []),
                        screenshot_png,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE playwright_state SET
                            logged_in = $1, browser_alive = $2,
                            usdc_balance = $3, positions_value = $4,
                            positions_json = $5, redeemable_json = $6,
                            history_json = $7,
                            updated_at = NOW()
                        WHERE id = 1
                        """,
                        logged_in, browser_alive,
                        usdc_balance, positions_value,
                        json.dumps(positions_json or []),
                        json.dumps(redeemable_json or []),
                        json.dumps(history_json or []),
                    )
        except Exception as e:
            log.error("db.playwright_state.error", error=str(e))

    async def check_redeem_requested(self) -> bool:
        """Check if a manual redeem was requested via Hub API."""
        if not self._pool:
            return False
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT redeem_requested FROM playwright_state WHERE id = 1"
                )
                if row and row["redeem_requested"]:
                    await conn.execute(
                        "UPDATE playwright_state SET redeem_requested = FALSE WHERE id = 1"
                    )
                    return True
                return False
        except Exception:
            return False

    async def write_redeem_event(self, result: dict) -> None:
        """Record a redeem sweep event."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO redeem_events (redeemed_count, failed_count, total_value, details_json)
                    VALUES ($1, $2, $3, $4)
                    """,
                    result.get("redeemed", 0),
                    result.get("failed", 0),
                    result.get("total_value", 0.0),
                    json.dumps(result.get("details", [])),
                )
        except Exception as e:
            log.error("db.redeem_event.error", error=str(e))

    # ─── Window Snapshots ────────────────────────────────────────────────────

    async def ensure_window_tables(self) -> None:
        """Create window_snapshots table if it doesn't exist."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS window_snapshots (
                        id SERIAL PRIMARY KEY,
                        window_ts BIGINT NOT NULL,
                        asset VARCHAR(10) NOT NULL,
                        timeframe VARCHAR(5) NOT NULL,
                        open_price DOUBLE PRECISION,
                        close_price DOUBLE PRECISION,
                        delta_pct DOUBLE PRECISION,
                        vpin DOUBLE PRECISION,
                        regime VARCHAR(20),
                        cg_connected BOOLEAN DEFAULT FALSE,
                        cg_oi_usd DOUBLE PRECISION,
                        cg_oi_delta_pct DOUBLE PRECISION,
                        cg_liq_long_usd DOUBLE PRECISION,
                        cg_liq_short_usd DOUBLE PRECISION,
                        cg_liq_total_usd DOUBLE PRECISION,
                        cg_long_pct DOUBLE PRECISION,
                        cg_short_pct DOUBLE PRECISION,
                        cg_long_short_ratio DOUBLE PRECISION,
                        cg_top_long_pct DOUBLE PRECISION,
                        cg_top_short_pct DOUBLE PRECISION,
                        cg_top_ratio DOUBLE PRECISION,
                        cg_taker_buy_usd DOUBLE PRECISION,
                        cg_taker_sell_usd DOUBLE PRECISION,
                        cg_funding_rate DOUBLE PRECISION,
                        direction VARCHAR(4),
                        confidence DOUBLE PRECISION,
                        cg_modifier DOUBLE PRECISION,
                        trade_placed BOOLEAN DEFAULT FALSE,
                        skip_reason VARCHAR(100),
                        outcome VARCHAR(4),
                        pnl_usd DOUBLE PRECISION,
                        poly_winner VARCHAR(10),
                        btc_price DOUBLE PRECISION,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        UNIQUE(window_ts, asset, timeframe)
                    );
                    CREATE INDEX IF NOT EXISTS idx_ws_ts ON window_snapshots(window_ts);
                    CREATE INDEX IF NOT EXISTS idx_ws_regime ON window_snapshots(regime);
                """)
            log.info("db.window_tables_ensured")
        except Exception as exc:
            log.error("db.ensure_window_tables_failed", error=str(exc))

    async def write_window_snapshot(self, snapshot: dict) -> None:
        """
        Persist a 5m/15m window evaluation snapshot.

        All fields are optional — missing keys default to None.
        Conflicts on (window_ts, asset, timeframe) are ignored (INSERT OR IGNORE semantics).
        """
        if not self._pool:
            return
        try:
            # Normalise confidence to float if it's a string
            confidence = snapshot.get("confidence")
            if isinstance(confidence, str):
                _conf_map = {"HIGH": 0.85, "MODERATE": 0.65, "LOW": 0.45, "NONE": 0.20}
                confidence = _conf_map.get(confidence.upper(), 0.5)

            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO window_snapshots (
                        window_ts, asset, timeframe,
                        open_price, close_price, delta_pct, vpin, regime,
                        cg_connected, cg_oi_usd, cg_oi_delta_pct,
                        cg_liq_long_usd, cg_liq_short_usd, cg_liq_total_usd,
                        cg_long_pct, cg_short_pct, cg_long_short_ratio,
                        cg_top_long_pct, cg_top_short_pct, cg_top_ratio,
                        cg_taker_buy_usd, cg_taker_sell_usd, cg_funding_rate,
                        direction, confidence, cg_modifier,
                        trade_placed, skip_reason,
                        outcome, pnl_usd, poly_winner, btc_price
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,
                        $9,$10,$11,$12,$13,$14,$15,$16,$17,
                        $18,$19,$20,$21,$22,$23,
                        $24,$25,$26,$27,$28,$29,$30,$31,$32
                    )
                    ON CONFLICT (window_ts, asset, timeframe) DO NOTHING
                    """,
                    snapshot.get("window_ts"),
                    snapshot.get("asset", "BTC"),
                    snapshot.get("timeframe", "5m"),
                    snapshot.get("open_price"),
                    snapshot.get("close_price"),
                    snapshot.get("delta_pct"),
                    snapshot.get("vpin"),
                    snapshot.get("regime"),
                    snapshot.get("cg_connected", False),
                    snapshot.get("cg_oi_usd"),
                    snapshot.get("cg_oi_delta_pct"),
                    snapshot.get("cg_liq_long_usd"),
                    snapshot.get("cg_liq_short_usd"),
                    snapshot.get("cg_liq_total_usd"),
                    snapshot.get("cg_long_pct"),
                    snapshot.get("cg_short_pct"),
                    snapshot.get("cg_long_short_ratio"),
                    snapshot.get("cg_top_long_pct"),
                    snapshot.get("cg_top_short_pct"),
                    snapshot.get("cg_top_ratio"),
                    snapshot.get("cg_taker_buy_usd"),
                    snapshot.get("cg_taker_sell_usd"),
                    snapshot.get("cg_funding_rate"),
                    snapshot.get("direction"),
                    confidence,
                    snapshot.get("cg_modifier"),
                    snapshot.get("trade_placed", False),
                    snapshot.get("skip_reason"),
                    snapshot.get("outcome"),
                    snapshot.get("pnl_usd"),
                    snapshot.get("poly_winner"),
                    snapshot.get("btc_price"),
                )
            log.debug(
                "db.window_snapshot_written",
                asset=snapshot.get("asset"),
                timeframe=snapshot.get("timeframe"),
                window_ts=snapshot.get("window_ts"),
            )
        except Exception as exc:
            log.error(
                "db.write_window_snapshot_failed",
                error=str(exc),
                asset=snapshot.get("asset"),
                window_ts=snapshot.get("window_ts"),
            )
            # Never re-raise — DB writes must not crash the engine

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
