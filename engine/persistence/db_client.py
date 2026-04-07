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
                    "live" if order.order_id.startswith("0x") else "paper",
                    not order.order_id.startswith("5min-") and not order.order_id.startswith("manual-paper"),
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

    async def update_gamma_prices(self, window_ts: int, asset: str, timeframe: str, gamma_up: float, gamma_down: float) -> None:
        """Store fresh T-60 Gamma prices to window_snapshot."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE window_snapshots SET gamma_up_price = $1, gamma_down_price = $2 WHERE window_ts = $3 AND asset = $4 AND timeframe = $5",
                    gamma_up, gamma_down, window_ts, asset, timeframe
                )
        except Exception:
            pass

    async def get_mode_toggles(self) -> dict | None:
        """Read paper_enabled / live_enabled from system_state (set by frontend toggle)."""
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT paper_enabled, live_enabled FROM system_state WHERE id = 1"
                )
                if row:
                    return {"paper_enabled": row["paper_enabled"], "live_enabled": row["live_enabled"]}
        except Exception:
            pass
        return None

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
                        -- TWAP data (v5.7)
                        twap_delta_pct DOUBLE PRECISION,
                        twap_direction VARCHAR(4),
                        twap_gamma_agree BOOLEAN,
                        twap_agreement_score INTEGER,
                        twap_confidence_boost DOUBLE PRECISION,
                        twap_n_ticks INTEGER,
                        twap_stability DOUBLE PRECISION,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        UNIQUE(window_ts, asset, timeframe)
                    );
                    CREATE INDEX IF NOT EXISTS idx_ws_ts ON window_snapshots(window_ts);
                    CREATE INDEX IF NOT EXISTS idx_ws_regime ON window_snapshots(regime);
                """)
                # Safe migration: add TWAP columns if table already exists (v5.7)
                for col, col_type in [
                    ("twap_delta_pct", "DOUBLE PRECISION"),
                    ("twap_direction", "VARCHAR(4)"),
                    ("twap_gamma_agree", "BOOLEAN"),
                    ("twap_agreement_score", "INTEGER"),
                    ("twap_confidence_boost", "DOUBLE PRECISION"),
                    ("twap_n_ticks", "INTEGER"),
                    ("twap_stability", "DOUBLE PRECISION"),
                    # v5.7c: trend + momentum + gamma gate
                    ("twap_trend_pct", "DOUBLE PRECISION"),
                    ("twap_momentum_pct", "DOUBLE PRECISION"),
                    ("twap_gamma_gate", "VARCHAR(12)"),
                    ("twap_should_skip", "BOOLEAN"),
                    ("twap_skip_reason", "VARCHAR(200)"),
                    # v6.0: TimesFM forecast data
                    ("timesfm_direction", "VARCHAR(4)"),
                    ("timesfm_confidence", "DOUBLE PRECISION"),
                    ("timesfm_predicted_close", "DOUBLE PRECISION"),
                    ("timesfm_delta_vs_open", "DOUBLE PRECISION"),
                    ("timesfm_spread", "DOUBLE PRECISION"),
                    ("timesfm_p10", "DOUBLE PRECISION"),
                    ("timesfm_p50", "DOUBLE PRECISION"),
                    ("timesfm_p90", "DOUBLE PRECISION"),
                    # v6.0: Spread/liquidity data
                    ("market_best_bid", "DOUBLE PRECISION"),
                    ("market_best_ask", "DOUBLE PRECISION"),
                    ("market_spread", "DOUBLE PRECISION"),
                    ("market_mid_price", "DOUBLE PRECISION"),
                    ("market_volume", "DOUBLE PRECISION"),
                    ("market_liquidity", "DOUBLE PRECISION"),
                    # v8.0: engine metadata + gate audit + shadow trade tracking
                    ("engine_version", "VARCHAR(10)"),
                    ("delta_source", "VARCHAR(10)"),
                    ("confidence_tier", "VARCHAR(10)"),
                    ("gates_passed", "TEXT"),
                    ("gate_failed", "VARCHAR(20)"),
                    ("shadow_trade_direction", "VARCHAR(4)"),
                    ("shadow_trade_entry_price", "DOUBLE PRECISION"),
                ]:
                    try:
                        await conn.execute(f"ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS {col} {col_type}")
                    except Exception:
                        pass  # Column already exists or not supported
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
                        outcome, pnl_usd, poly_winner, btc_price,
                        twap_delta_pct, twap_direction, twap_gamma_agree,
                        twap_agreement_score, twap_confidence_boost,
                        twap_n_ticks, twap_stability,
                        twap_trend_pct, twap_momentum_pct, twap_gamma_gate,
                        twap_should_skip, twap_skip_reason,
                        timesfm_direction, timesfm_confidence,
                        timesfm_predicted_close, timesfm_delta_vs_open,
                        timesfm_spread, timesfm_p10, timesfm_p50, timesfm_p90,
                        market_best_bid, market_best_ask,
                        market_spread, market_mid_price,
                        market_volume, market_liquidity,
                        v71_would_trade, v71_skip_reason, v71_regime,
                        is_live,
                        gamma_up_price, gamma_down_price,
                        delta_chainlink, delta_tiingo, delta_binance, price_consensus,
                        engine_version, delta_source, confidence_tier,
                        gates_passed, gate_failed,
                        shadow_trade_direction, shadow_trade_entry_price,
                        v2_probability_up, v2_direction, v2_agrees,
                        v2_model_version, eval_offset,
                        v2_quantiles, v2_quantiles_at_close
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,
                        $9,$10,$11,$12,$13,$14,$15,$16,$17,
                        $18,$19,$20,$21,$22,$23,
                        $24,$25,$26,$27,$28,$29,$30,$31,$32,
                        $33,$34,$35,$36,$37,$38,$39,
                        $40,$41,$42,$43,$44,
                        $45,$46,$47,$48,$49,$50,$51,$52,
                        $53,$54,$55,$56,$57,$58,
                        $59,$60,$61,
                        $62,
                        $63,$64,
                        $65,$66,$67,$68,
                        $69,$70,$71,
                        $72,$73,
                        $74,$75,$76,$77,$78,
                        $79,$80,$81,$82
                    )
                    ON CONFLICT (window_ts, asset, timeframe, eval_offset) DO UPDATE SET
                        gamma_up_price         = COALESCE(EXCLUDED.gamma_up_price, window_snapshots.gamma_up_price),
                        gamma_down_price       = COALESCE(EXCLUDED.gamma_down_price, window_snapshots.gamma_down_price),
                        delta_chainlink        = COALESCE(EXCLUDED.delta_chainlink, window_snapshots.delta_chainlink),
                        delta_tiingo           = COALESCE(EXCLUDED.delta_tiingo, window_snapshots.delta_tiingo),
                        delta_binance          = COALESCE(EXCLUDED.delta_binance, window_snapshots.delta_binance),
                        price_consensus        = COALESCE(EXCLUDED.price_consensus, window_snapshots.price_consensus),
                        engine_version         = COALESCE(EXCLUDED.engine_version, window_snapshots.engine_version),
                        delta_source           = COALESCE(EXCLUDED.delta_source, window_snapshots.delta_source),
                        confidence_tier        = COALESCE(EXCLUDED.confidence_tier, window_snapshots.confidence_tier),
                        gates_passed           = COALESCE(EXCLUDED.gates_passed, window_snapshots.gates_passed),
                        gate_failed            = COALESCE(EXCLUDED.gate_failed, window_snapshots.gate_failed),
                        shadow_trade_direction = COALESCE(EXCLUDED.shadow_trade_direction, window_snapshots.shadow_trade_direction),
                        shadow_trade_entry_price = COALESCE(EXCLUDED.shadow_trade_entry_price, window_snapshots.shadow_trade_entry_price),
                        v2_probability_up      = COALESCE(EXCLUDED.v2_probability_up, window_snapshots.v2_probability_up),
                        v2_direction           = COALESCE(EXCLUDED.v2_direction, window_snapshots.v2_direction),
                        v2_agrees              = COALESCE(EXCLUDED.v2_agrees, window_snapshots.v2_agrees),
                        v2_model_version       = COALESCE(EXCLUDED.v2_model_version, window_snapshots.v2_model_version),
                        eval_offset            = COALESCE(EXCLUDED.eval_offset, window_snapshots.eval_offset),
                        v2_quantiles           = COALESCE(EXCLUDED.v2_quantiles, window_snapshots.v2_quantiles),
                        v2_quantiles_at_close  = COALESCE(EXCLUDED.v2_quantiles_at_close, window_snapshots.v2_quantiles_at_close)
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
                    snapshot.get("twap_delta_pct"),
                    snapshot.get("twap_direction"),
                    snapshot.get("twap_gamma_agree"),
                    snapshot.get("twap_agreement_score"),
                    snapshot.get("twap_confidence_boost"),
                    snapshot.get("twap_n_ticks"),
                    snapshot.get("twap_stability"),
                    # v5.7c: trend + momentum + gamma gate
                    snapshot.get("twap_trend_pct"),
                    snapshot.get("twap_momentum_pct"),
                    snapshot.get("twap_gamma_gate"),
                    snapshot.get("twap_should_skip"),
                    snapshot.get("twap_skip_reason"),
                    # v6.0: TimesFM forecast
                    snapshot.get("timesfm_direction"),
                    snapshot.get("timesfm_confidence"),
                    snapshot.get("timesfm_predicted_close"),
                    snapshot.get("timesfm_delta_vs_open"),
                    snapshot.get("timesfm_spread"),
                    snapshot.get("timesfm_p10"),
                    snapshot.get("timesfm_p50"),
                    snapshot.get("timesfm_p90"),
                    # v6.0: Spread/liquidity
                    snapshot.get("market_best_bid"),
                    snapshot.get("market_best_ask"),
                    snapshot.get("market_spread"),
                    snapshot.get("market_mid_price"),
                    snapshot.get("market_volume"),
                    snapshot.get("market_liquidity"),
                    snapshot.get("v71_would_trade"),
                    snapshot.get("v71_skip_reason"),
                    snapshot.get("v71_regime"),
                    snapshot.get("is_live", False),
                    # gamma prices (fetched at T-60 and included in snapshot dict)
                    snapshot.get("gamma_up_price"),
                    snapshot.get("gamma_down_price"),
                    # v7.2: multi-source deltas
                    snapshot.get("delta_chainlink"),
                    snapshot.get("delta_tiingo"),
                    snapshot.get("delta_binance"),
                    snapshot.get("price_consensus"),
                    # v8.0: engine metadata + gate audit + shadow trade
                    snapshot.get("engine_version", "v8.0"),
                    snapshot.get("delta_source"),
                    snapshot.get("confidence_tier"),
                    snapshot.get("gates_passed"),
                    snapshot.get("gate_failed"),
                    snapshot.get("shadow_trade_direction"),
                    snapshot.get("shadow_trade_entry_price"),
                    # v8.1: OAK (v2.2) early entry gate
                    snapshot.get("v2_probability_up"),
                    snapshot.get("v2_direction"),
                    snapshot.get("v2_agrees"),
                    snapshot.get("v2_model_version"),
                    snapshot.get("eval_offset"),
                    snapshot.get("v2_quantiles"),
                    snapshot.get("v2_quantiles_at_close"),
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

    async def update_window_outcome(
        self,
        window_ts,
        asset: str,
        timeframe: str,
        outcome: str,
        pnl_usd: float,
        poly_winner=None,
    ) -> None:
        """Update a window_snapshot with resolution data (outcome, PnL, poly_winner)."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE window_snapshots
                    SET outcome = $1, pnl_usd = $2, poly_winner = $3
                    WHERE window_ts = $4 AND asset = $5 AND timeframe = $6
                    """,
                    outcome,
                    pnl_usd,
                    poly_winner,
                    window_ts,
                    asset,
                    timeframe,
                )
            log.debug(
                "db.window_outcome_updated",
                window_ts=window_ts,
                asset=asset,
                timeframe=timeframe,
                outcome=outcome,
                pnl_usd=pnl_usd,
            )
        except Exception as exc:
            log.error("db.update_window_outcome_failed", error=str(exc))

    async def update_window_prices(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        **kwargs,
    ) -> None:
        """Update price columns on window_snapshot (chainlink, tiingo, poly resolution)."""
        if not self._pool:
            return
        valid_cols = {
            "chainlink_open", "chainlink_close", "tiingo_open", "tiingo_close",
            "poly_resolved_outcome", "poly_up_price_final", "poly_down_price_final",
        }
        updates = []
        params = []
        idx = 4  # $1=window_ts, $2=asset, $3=timeframe
        for col, val in kwargs.items():
            if col in valid_cols and val is not None:
                idx += 1
                updates.append(f"{col} = ${idx}")
                params.append(val)
        if not updates:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE window_snapshots SET {', '.join(updates)} "
                    f"WHERE window_ts = $1 AND asset = $2 AND timeframe = $3",
                    window_ts, asset, timeframe, *params,
                )
        except Exception as exc:
            log.error("db.update_window_prices_failed", error=str(exc)[:80])

    async def update_window_resolution_extras(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        binance_close: Optional[float] = None,
        chainlink_binance_direction_match: Optional[bool] = None,
        resolution_delay_secs: Optional[int] = None,
    ) -> None:
        """Update extra resolution columns (v7.2: Binance close, direction match, delay)."""
        if not self._pool:
            return
        updates = []
        params = []
        idx = 4
        if binance_close is not None:
            idx += 1
            updates.append(f"binance_close = ${idx}")
            params.append(binance_close)
        if chainlink_binance_direction_match is not None:
            idx += 1
            updates.append(f"chainlink_binance_direction_match = ${idx}")
            params.append(chainlink_binance_direction_match)
        if resolution_delay_secs is not None:
            idx += 1
            updates.append(f"resolution_delay_secs = ${idx}")
            params.append(resolution_delay_secs)
        if not updates:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE window_snapshots SET {', '.join(updates)} "
                    f"WHERE window_ts = $1 AND asset = $2 AND timeframe = $3",
                    window_ts, asset, timeframe, *params,
                )
        except Exception as exc:
            log.error("db.update_window_resolution_extras_failed", error=str(exc)[:80])

    async def get_latest_chainlink_price(self, asset: str = "BTC") -> float | None:
        """Get the most recent Chainlink price for an asset."""
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT price FROM ticks_chainlink WHERE asset = $1 ORDER BY ts DESC LIMIT 1",
                    asset,
                )
                return float(row["price"]) if row else None
        except Exception:
            return None

    async def get_latest_tiingo_price(self, asset: str = "BTC") -> float | None:
        """Get the most recent Tiingo price for an asset."""
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT last_price FROM ticks_tiingo WHERE asset = $1 ORDER BY ts DESC LIMIT 1",
                    asset,
                )
                return float(row["last_price"]) if row else None
        except Exception:
            return None

    async def get_latest_macro_signal(self) -> dict | None:
        """Get the most recent macro observer signal (< 5 min old)."""
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT bias, confidence, direction_gate, reasoning "
                    "FROM macro_signals "
                    "WHERE created_at > NOW() - INTERVAL '5 minutes' "
                    "ORDER BY created_at DESC LIMIT 1",
                )
                if row:
                    return {
                        "macro_bias": row["bias"],
                        "macro_confidence": f"{row['confidence']}%",
                        "macro_gate": row["direction_gate"],
                        "macro_reasoning": row["reasoning"][:100] if row["reasoning"] else "",
                    }
                return None
        except Exception:
            return None

    async def get_latest_clob_prices(self, asset: str = "BTC") -> dict | None:
        """Get the most recent CLOB book prices for an asset."""
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT up_best_bid, up_best_ask, down_best_bid, down_best_ask "
                    "FROM ticks_clob WHERE asset = $1 ORDER BY ts DESC LIMIT 1",
                    asset,
                )
                if row:
                    return {
                        "clob_up_bid": float(row["up_best_bid"]) if row["up_best_bid"] else None,
                        "clob_up_ask": float(row["up_best_ask"]) if row["up_best_ask"] else None,
                        "clob_down_bid": float(row["down_best_bid"]) if row["down_best_bid"] else None,
                        "clob_down_ask": float(row["down_best_ask"]) if row["down_best_ask"] else None,
                    }
                return None
        except Exception:
            return None

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

    async def get_open_trades(self, hours_back: int = 24) -> list[dict]:
        """Get OPEN trades from the last N hours for recovery on startup.

        Args:
            hours_back: How many hours back to look (default 24).

        Returns:
            List of trade row dicts with keys: order_id, direction, entry_price,
            stake_usd, metadata, created_at, strategy, venue, market_slug, fee_usd.
        """
        if not self._pool:
            return []
        try:
            from datetime import timezone, timedelta
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

    async def mark_trade_expired(self, order_id: str) -> None:
        """Mark a trade as EXPIRED in the DB (used by startup reconciliation)."""
        if not self._pool:
            return
        try:
            from datetime import timezone
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE trades SET status = 'EXPIRED', resolved_at = $1 WHERE order_id = $2",
                    datetime.now(timezone.utc),
                    order_id,
                )
            log.info("db.trade_marked_expired", order_id=order_id[:24] if len(order_id) > 24 else order_id)
        except Exception as exc:
            log.error("db.mark_trade_expired_failed", order_id=order_id, error=str(exc))

    # ─── Manual Trade Queue (v5.8 Dashboard) ─────────────────────────────

    async def poll_pending_live_trades(self) -> list:
        """Fetch manual trades with status='pending_live' for engine execution."""
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

    async def update_manual_trade_status(
        self, trade_id: str, status: str, pnl_usd: float = None,
        outcome_direction: str = None, clob_order_id: str = None,
    ) -> None:
        """Update a manual trade after execution or resolution."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                if pnl_usd is not None:
                    await conn.execute("""
                        UPDATE manual_trades
                        SET status = $1, pnl_usd = $2, outcome_direction = $3, resolved_at = NOW()
                        WHERE trade_id = $4
                    """, status, pnl_usd, outcome_direction, trade_id)
                else:
                    await conn.execute("""
                        UPDATE manual_trades SET status = $1 WHERE trade_id = $2
                    """, status, trade_id)
            log.info("db.manual_trade_updated", trade_id=trade_id, status=status)
        except Exception as exc:
            log.error("db.manual_trade_update_failed", error=str(exc))

    async def get_window_close(self, window_ts: int, asset: str, timeframe: str) -> float:
        """Get the close price for a resolved window from window_snapshots."""
        if not self._pool:
            return 0.0
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT close_price FROM window_snapshots WHERE window_ts = $1 AND asset = $2 AND timeframe = $3",
                    window_ts, asset, timeframe
                )
                return float(row) if row else 0.0
        except Exception:
            return 0.0

    async def update_window_trade_placed(self, window_ts: int, asset: str, timeframe: str) -> None:
        """Mark a window_snapshot as having a trade placed."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE window_snapshots SET trade_placed = TRUE WHERE window_ts = $1 AND asset = $2 AND timeframe = $3",
                    window_ts, asset, timeframe
                )
                import structlog
                structlog.get_logger().info("db.trade_placed_updated", window_ts=window_ts, asset=asset, result=result)
        except Exception as exc:
            import structlog
            structlog.get_logger().error("db.trade_placed_update_failed", window_ts=window_ts, asset=asset, error=str(exc))

    async def update_window_fok_data(
        self, window_ts: int, asset: str, timeframe: str,
        execution_mode: str, fok_attempts: int, fok_fill_step: int, clob_fill_price: float,
    ) -> None:
        """Write FOK execution details to window_snapshot after a successful fill."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """UPDATE window_snapshots
                       SET execution_mode = $4, fok_attempts = $5,
                           fok_fill_step = $6, clob_fill_price = $7
                       WHERE window_ts = $1 AND asset = $2 AND timeframe = $3""",
                    window_ts, asset, timeframe,
                    execution_mode, fok_attempts, fok_fill_step, clob_fill_price,
                )
        except Exception as exc:
            import structlog
            structlog.get_logger().error("db.fok_data_update_failed", window_ts=window_ts, error=str(exc))

    async def update_window_skip_reason(self, window_ts: int, asset: str, timeframe: str, skip_reason: str) -> None:
        """Update skip_reason on a window_snapshot after evaluation."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE window_snapshots SET skip_reason = $1 WHERE window_ts = $2 AND asset = $3 AND timeframe = $4",
                    skip_reason, window_ts, asset, timeframe
                )
        except Exception:
            pass

    async def write_countdown_evaluation(self, data: dict) -> None:
        """
        Persist a multi-stage countdown snapshot to countdown_evaluations table.

        Args:
            data: dict with keys:
                window_ts, stage, direction, confidence, agreement, action, notes,
                chainlink_price, tiingo_price, binance_price  (v7.2: multi-source prices)
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO countdown_evaluations
                       (window_ts, stage, direction, confidence, agreement, action, notes, evaluated_at,
                        chainlink_price, tiingo_price, binance_price)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), $8, $9, $10)""",
                    int(data.get("window_ts", 0)),
                    data.get("stage", ""),
                    data.get("direction"),
                    float(data.get("confidence", 0)) if data.get("confidence") is not None else None,
                    bool(data.get("agreement")) if data.get("agreement") is not None else None,
                    data.get("action"),
                    data.get("notes"),
                    float(data.get("chainlink_price")) if data.get("chainlink_price") is not None else None,
                    float(data.get("tiingo_price")) if data.get("tiingo_price") is not None else None,
                    float(data.get("binance_price")) if data.get("binance_price") is not None else None,
                )
        except Exception as exc:
            log.debug("db.write_countdown_evaluation_failed", error=str(exc)[:120])

    async def write_evaluation(self, data: dict) -> None:
        """
        Write a Claude evaluation to countdown_evaluations (compatibility shim).
        Maps claude_evaluator's write_evaluation call to the countdown_evaluations table.
        """
        await self.write_countdown_evaluation({
            "window_ts": int(data.get("timestamp", 0)),
            "stage": "claude_eval",
            "direction": data.get("direction"),
            "confidence": data.get("confidence"),
            "agreement": data.get("trade_placed"),
            "action": "TRADE" if data.get("trade_placed") else "SKIP",
            "notes": data.get("analysis", "")[:2000] if data.get("analysis") else None,
        })

    # ─── Shadow Trade Resolution ──────────────────────────────────────────────

    async def ensure_shadow_columns(self) -> None:
        """Add shadow trade resolution columns to window_snapshots if missing."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                for col, col_type in [
                    ("shadow_trade_direction", "VARCHAR(4)"),
                    ("shadow_trade_entry_price", "DOUBLE PRECISION"),
                    ("oracle_outcome", "VARCHAR(4)"),
                    ("shadow_pnl", "DOUBLE PRECISION"),
                    ("shadow_would_win", "BOOLEAN"),
                ]:
                    await conn.execute(
                        f"ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    )
            log.info("db.shadow_columns_ensured")
        except Exception as exc:
            log.warning("db.ensure_shadow_columns_failed", error=str(exc))

    async def ensure_v8_trade_columns(self) -> None:
        """Add v8.0 columns to trades table if missing (idempotent)."""
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

    async def get_unresolved_shadow_windows(self, minutes_back: int = 10) -> list:
        """
        Get recent skipped windows that haven't been shadow-resolved yet.

        Returns window_snapshots rows where:
          - trade_placed = FALSE (skipped)
          - shadow_trade_direction IS NOT NULL
          - oracle_outcome IS NULL (not yet resolved)
          - window_ts > now() - interval (recent enough to query)
        """
        if not self._pool:
            return []
        try:
            cutoff_ts = int(__import__("time").time()) - (minutes_back * 60)
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT window_ts, asset, timeframe,
                           shadow_trade_direction, shadow_trade_entry_price,
                           skip_reason, confidence
                    FROM window_snapshots
                    WHERE trade_placed = FALSE
                      AND shadow_trade_direction IS NOT NULL
                      AND oracle_outcome IS NULL
                      AND window_ts > $1
                    ORDER BY window_ts DESC
                    LIMIT 20
                    """,
                    cutoff_ts,
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("db.get_unresolved_shadow_windows_failed", error=str(exc))
            return []

    async def update_shadow_resolution(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        oracle_outcome: str,
        shadow_pnl: float,
        shadow_would_win: bool,
    ) -> None:
        """
        Update a skipped window with oracle resolution for shadow trade analysis.

        Args:
            window_ts:       Window open timestamp (unix int)
            asset:           e.g. "BTC"
            timeframe:       e.g. "5m"
            oracle_outcome:  "UP" or "DOWN" (what Polymarket oracle resolved)
            shadow_pnl:      Simulated P&L if the trade had been placed
            shadow_would_win: True if shadow_trade_direction matched oracle_outcome
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE window_snapshots
                    SET oracle_outcome  = $1,
                        shadow_pnl      = $2,
                        shadow_would_win = $3
                    WHERE window_ts = $4 AND asset = $5 AND timeframe = $6
                    """,
                    oracle_outcome,
                    shadow_pnl,
                    shadow_would_win,
                    window_ts,
                    asset,
                    timeframe,
                )
            log.debug(
                "db.shadow_resolution_updated",
                window_ts=window_ts,
                asset=asset,
                oracle_outcome=oracle_outcome,
                shadow_pnl=f"{shadow_pnl:+.2f}",
                shadow_would_win=shadow_would_win,
            )
        except Exception as exc:
            log.error("db.update_shadow_resolution_failed", error=str(exc))

    async def write_gate_audit(self, data: dict) -> None:
        """
        Write a gate audit record for every window evaluation (v8.0).

        Records all gate pass/fail results so signal analysis can identify which
        gate is blocking trades and whether skipped windows would have been winners.

        Args:
            data: dict with keys matching gate_audit table columns.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO gate_audit (
                        window_ts, asset, timeframe, engine_version,
                        delta_source, eval_offset,
                        open_price, tiingo_open, tiingo_close,
                        delta_tiingo, delta_binance, delta_chainlink, delta_pct,
                        vpin, regime,
                        gate_vpin, gate_delta, gate_cg, gate_floor, gate_cap,
                        gate_passed, gate_failed, gates_passed_list,
                        decision, skip_reason
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $25,
                        $6, $7, $8,
                        $9, $10, $11, $12,
                        $13, $14,
                        $15, $16, $17, $18, $19,
                        $20, $21, $22,
                        $23, $24
                    )
                    ON CONFLICT (window_ts, asset, timeframe, eval_offset) DO UPDATE SET
                        engine_version      = EXCLUDED.engine_version,
                        delta_source        = EXCLUDED.delta_source,
                        open_price          = EXCLUDED.open_price,
                        tiingo_open         = EXCLUDED.tiingo_open,
                        tiingo_close        = EXCLUDED.tiingo_close,
                        delta_tiingo        = EXCLUDED.delta_tiingo,
                        delta_binance       = EXCLUDED.delta_binance,
                        delta_chainlink     = EXCLUDED.delta_chainlink,
                        delta_pct           = EXCLUDED.delta_pct,
                        vpin                = EXCLUDED.vpin,
                        regime              = EXCLUDED.regime,
                        gate_vpin           = EXCLUDED.gate_vpin,
                        gate_delta          = EXCLUDED.gate_delta,
                        gate_cg             = EXCLUDED.gate_cg,
                        gate_floor          = EXCLUDED.gate_floor,
                        gate_cap            = EXCLUDED.gate_cap,
                        gate_passed         = EXCLUDED.gate_passed,
                        gate_failed         = EXCLUDED.gate_failed,
                        gates_passed_list   = EXCLUDED.gates_passed_list,
                        decision            = EXCLUDED.decision,
                        skip_reason         = EXCLUDED.skip_reason,
                        evaluated_at        = NOW()
                    """,
                    int(data.get("window_ts", 0)),
                    data.get("asset", "BTC"),
                    data.get("timeframe", "5m"),
                    data.get("engine_version", "v8.0"),
                    data.get("delta_source"),
                    float(data["open_price"]) if data.get("open_price") is not None else None,
                    float(data["tiingo_open"]) if data.get("tiingo_open") is not None else None,
                    float(data["tiingo_close"]) if data.get("tiingo_close") is not None else None,
                    float(data["delta_tiingo"]) if data.get("delta_tiingo") is not None else None,
                    float(data["delta_binance"]) if data.get("delta_binance") is not None else None,
                    float(data["delta_chainlink"]) if data.get("delta_chainlink") is not None else None,
                    float(data["delta_pct"]) if data.get("delta_pct") is not None else None,
                    float(data["vpin"]) if data.get("vpin") is not None else None,
                    data.get("regime"),
                    str(data["gate_vpin"]) if data.get("gate_vpin") is not None else None,  # VARCHAR
                    str(data["gate_delta"]) if data.get("gate_delta") is not None else None,  # VARCHAR
                    bool(data["gate_cg"]) if data.get("gate_cg") is not None else None,  # BOOLEAN
                    str(data["gate_floor"]) if data.get("gate_floor") is not None else None,  # VARCHAR
                    str(data["gate_cap"]) if data.get("gate_cap") is not None else None,  # VARCHAR
                    bool(data.get("gate_passed", False)),
                    data.get("gate_failed"),
                    data.get("gates_passed_list"),
                    data.get("decision", "SKIP"),
                    data.get("skip_reason"),
                    data.get("eval_offset"),
                )
        except Exception as exc:
            log.warning("db.write_gate_audit_failed", error=str(exc)[:200])

    async def write_signal_evaluation(self, data: dict) -> None:
        """
        Write comprehensive signal evaluation data for every window evaluation point.
        
        Captures ALL signal data at each eval_offset: all price sources, all deltas,
        OAK full probability surface (quantiles), all gates, and market microstructure.
        
        Args:
            data: dict with keys matching signal_evaluations table columns.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO signal_evaluations (
                        window_ts, asset, timeframe, eval_offset,
                        clob_up_bid, clob_up_ask, clob_down_bid, clob_down_ask,
                        binance_price, tiingo_open, tiingo_close, chainlink_price,
                        delta_pct, delta_tiingo, delta_binance, delta_chainlink, delta_source,
                        vpin, regime, clob_spread, clob_mid,
                        v2_probability_up, v2_direction, v2_agrees, v2_high_conf,
                        v2_model_version, v2_quantiles, v2_quantiles_at_close,
                        gate_vpin_passed, gate_delta_passed, gate_cg_passed,
                        gate_twap_passed, gate_timesfm_passed, gate_passed,
                        gate_failed, decision,
                        twap_delta, twap_direction, twap_gamma_agree
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10, $11, $12,
                        $13, $14, $15, $16, $17,
                        $18, $19, $20, $21,
                        $22, $23, $24, $25,
                        $26, $27, $28,
                        $29, $30, $31,
                        $32, $33, $34, $35,
                        $36, $37, $38, $39
                    )
                    ON CONFLICT (window_ts, asset, timeframe, eval_offset) DO UPDATE SET
                        clob_up_bid           = EXCLUDED.clob_up_bid,
                        clob_up_ask           = EXCLUDED.clob_up_ask,
                        clob_down_bid         = EXCLUDED.clob_down_bid,
                        clob_down_ask         = EXCLUDED.clob_down_ask,
                        binance_price         = EXCLUDED.binance_price,
                        tiingo_open           = EXCLUDED.tiingo_open,
                        tiingo_close          = EXCLUDED.tiingo_close,
                        chainlink_price       = EXCLUDED.chainlink_price,
                        delta_pct             = EXCLUDED.delta_pct,
                        delta_tiingo          = EXCLUDED.delta_tiingo,
                        delta_binance         = EXCLUDED.delta_binance,
                        delta_chainlink       = EXCLUDED.delta_chainlink,
                        delta_source          = EXCLUDED.delta_source,
                        vpin                  = EXCLUDED.vpin,
                        regime                = EXCLUDED.regime,
                        clob_spread           = EXCLUDED.clob_spread,
                        clob_mid              = EXCLUDED.clob_mid,
                        v2_probability_up     = EXCLUDED.v2_probability_up,
                        v2_direction          = EXCLUDED.v2_direction,
                        v2_agrees             = EXCLUDED.v2_agrees,
                        v2_high_conf          = EXCLUDED.v2_high_conf,
                        v2_model_version      = EXCLUDED.v2_model_version,
                        v2_quantiles          = EXCLUDED.v2_quantiles,
                        v2_quantiles_at_close = EXCLUDED.v2_quantiles_at_close,
                        gate_vpin_passed      = EXCLUDED.gate_vpin_passed,
                        gate_delta_passed     = EXCLUDED.gate_delta_passed,
                        gate_cg_passed        = EXCLUDED.gate_cg_passed,
                        gate_twap_passed      = EXCLUDED.gate_twap_passed,
                        gate_timesfm_passed   = EXCLUDED.gate_timesfm_passed,
                        gate_passed           = EXCLUDED.gate_passed,
                        gate_failed           = EXCLUDED.gate_failed,
                        decision              = EXCLUDED.decision,
                        twap_delta            = EXCLUDED.twap_delta,
                        twap_direction        = EXCLUDED.twap_direction,
                        twap_gamma_agree      = EXCLUDED.twap_gamma_agree,
                        evaluated_at          = NOW()
                    """,
                    int(data.get("window_ts", 0)),
                    data.get("asset", "BTC"),
                    data.get("timeframe", "5m"),
                    data.get("eval_offset"),
                    float(data["clob_up_bid"]) if data.get("clob_up_bid") is not None else None,
                    float(data["clob_up_ask"]) if data.get("clob_up_ask") is not None else None,
                    float(data["clob_down_bid"]) if data.get("clob_down_bid") is not None else None,
                    float(data["clob_down_ask"]) if data.get("clob_down_ask") is not None else None,
                    float(data["binance_price"]) if data.get("binance_price") is not None else None,
                    float(data["tiingo_open"]) if data.get("tiingo_open") is not None else None,
                    float(data["tiingo_close"]) if data.get("tiingo_close") is not None else None,
                    float(data["chainlink_price"]) if data.get("chainlink_price") is not None else None,
                    float(data["delta_pct"]) if data.get("delta_pct") is not None else None,
                    float(data["delta_tiingo"]) if data.get("delta_tiingo") is not None else None,
                    float(data["delta_binance"]) if data.get("delta_binance") is not None else None,
                    float(data["delta_chainlink"]) if data.get("delta_chainlink") is not None else None,
                    data.get("delta_source"),
                    float(data["vpin"]) if data.get("vpin") is not None else None,
                    data.get("regime"),
                    float(data["clob_spread"]) if data.get("clob_spread") is not None else None,
                    float(data["clob_mid"]) if data.get("clob_mid") is not None else None,
                    float(data["v2_probability_up"]) if data.get("v2_probability_up") is not None else None,
                    data.get("v2_direction"),
                    bool(data["v2_agrees"]) if data.get("v2_agrees") is not None else None,
                    bool(data["v2_high_conf"]) if data.get("v2_high_conf") is not None else None,
                    data.get("v2_model_version"),
                    data.get("v2_quantiles"),  # JSONB (already serialized as JSON string)
                    data.get("v2_quantiles_at_close"),  # JSONB
                    bool(data["gate_vpin_passed"]) if data.get("gate_vpin_passed") is not None else None,
                    bool(data["gate_delta_passed"]) if data.get("gate_delta_passed") is not None else None,
                    bool(data["gate_cg_passed"]) if data.get("gate_cg_passed") is not None else None,
                    bool(data["gate_twap_passed"]) if data.get("gate_twap_passed") is not None else None,
                    bool(data["gate_timesfm_passed"]) if data.get("gate_timesfm_passed") is not None else None,
                    bool(data.get("gate_passed", False)),
                    data.get("gate_failed"),
                    data.get("decision", "SKIP"),
                    float(data["twap_delta"]) if data.get("twap_delta") is not None else None,
                    data.get("twap_direction"),
                    bool(data["twap_gamma_agree"]) if data.get("twap_gamma_agree") is not None else None
                )
        except Exception as exc:
            log.warning("db.write_signal_evaluation_failed", error=str(exc)[:200])

    # ── Post-Resolution AI Analysis ──────────────────────────────────────────

    async def ensure_post_resolution_table(self) -> None:
        """
        Ensure post_resolution_analyses table exists (idempotent).
        Also adds ai_post_analysis columns to window_snapshots if missing.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS post_resolution_analyses (
                        id                SERIAL PRIMARY KEY,
                        window_ts         BIGINT NOT NULL,
                        asset             VARCHAR(10) NOT NULL DEFAULT 'BTC',
                        timeframe         VARCHAR(5)  NOT NULL DEFAULT '5m',
                        oracle_direction  VARCHAR(4),
                        n_ticks           INTEGER DEFAULT 0,
                        missed_profit_usd DOUBLE PRECISION DEFAULT 0,
                        blocked_loss_usd  DOUBLE PRECISION DEFAULT 0,
                        cap_too_tight     BOOLEAN DEFAULT FALSE,
                        gate_recommendation TEXT,
                        ai_post_analysis  TEXT,
                        analysed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (window_ts, asset, timeframe)
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pra_window_ts ON post_resolution_analyses(window_ts)"
                )
                # Also add summary columns to window_snapshots for quick access
                for col, col_type in [
                    ("ai_post_analysis", "TEXT"),
                    ("missed_profit_usd", "DOUBLE PRECISION"),
                    ("blocked_loss_usd", "DOUBLE PRECISION"),
                    ("cap_too_tight", "BOOLEAN"),
                    ("gate_recommendation", "TEXT"),
                ]:
                    await conn.execute(
                        f"ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    )
            log.info("db.post_resolution_table_ensured")
        except Exception as exc:
            log.warning("db.ensure_post_resolution_table_failed", error=str(exc))

    # ── Window Predictions (Tiingo + Chainlink at T-0) ───────────────────

    async def ensure_window_predictions_table(self) -> None:
        """Create window_predictions table for tracking predicted vs actual outcomes."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS window_predictions (
                        window_ts         BIGINT NOT NULL,
                        asset             VARCHAR(10) NOT NULL DEFAULT 'BTC',
                        timeframe         VARCHAR(5)  NOT NULL DEFAULT '5m',
                        tiingo_open       DOUBLE PRECISION,
                        tiingo_close      DOUBLE PRECISION,
                        chainlink_open    DOUBLE PRECISION,
                        chainlink_close   DOUBLE PRECISION,
                        tiingo_direction  VARCHAR(4),
                        chainlink_direction VARCHAR(4),
                        our_signal_direction VARCHAR(4),
                        v2_direction      VARCHAR(4),
                        v2_probability    DOUBLE PRECISION,
                        vpin_at_close     DOUBLE PRECISION,
                        regime            VARCHAR(15),
                        trade_placed      BOOLEAN DEFAULT FALSE,
                        our_direction     VARCHAR(4),
                        our_entry_price   DOUBLE PRECISION,
                        bid_unfilled      BOOLEAN DEFAULT FALSE,
                        skip_reason       TEXT,
                        oracle_winner     VARCHAR(4),
                        tiingo_correct    BOOLEAN,
                        chainlink_correct BOOLEAN,
                        our_signal_correct BOOLEAN,
                        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (window_ts, asset, timeframe)
                    )
                """)
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_wp_window_ts ON window_predictions(window_ts)"
                )
            log.info("db.window_predictions_table_ensured")
        except Exception as exc:
            log.warning("db.ensure_window_predictions_failed", error=str(exc))

    async def write_window_prediction(self, data: dict) -> None:
        """Write or update a window prediction record."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO window_predictions (
                        window_ts, asset, timeframe,
                        tiingo_open, tiingo_close,
                        chainlink_open, chainlink_close,
                        tiingo_direction, chainlink_direction,
                        our_signal_direction, v2_direction, v2_probability,
                        vpin_at_close, regime,
                        trade_placed, our_direction, our_entry_price,
                        bid_unfilled, skip_reason
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                    ON CONFLICT (window_ts, asset, timeframe) DO UPDATE SET
                        tiingo_close = EXCLUDED.tiingo_close,
                        chainlink_close = EXCLUDED.chainlink_close,
                        tiingo_direction = EXCLUDED.tiingo_direction,
                        chainlink_direction = EXCLUDED.chainlink_direction,
                        our_signal_direction = EXCLUDED.our_signal_direction,
                        v2_direction = EXCLUDED.v2_direction,
                        v2_probability = EXCLUDED.v2_probability,
                        vpin_at_close = EXCLUDED.vpin_at_close,
                        regime = EXCLUDED.regime,
                        trade_placed = EXCLUDED.trade_placed,
                        our_direction = EXCLUDED.our_direction,
                        our_entry_price = EXCLUDED.our_entry_price,
                        bid_unfilled = EXCLUDED.bid_unfilled,
                        skip_reason = EXCLUDED.skip_reason
                """,
                    int(data.get("window_ts", 0)),
                    data.get("asset", "BTC"),
                    data.get("timeframe", "5m"),
                    data.get("tiingo_open"),
                    data.get("tiingo_close"),
                    data.get("chainlink_open"),
                    data.get("chainlink_close"),
                    data.get("tiingo_direction"),
                    data.get("chainlink_direction"),
                    data.get("our_signal_direction"),
                    data.get("v2_direction"),
                    data.get("v2_probability"),
                    data.get("vpin_at_close"),
                    data.get("regime"),
                    data.get("trade_placed", False),
                    data.get("our_direction"),
                    data.get("our_entry_price"),
                    data.get("bid_unfilled", False),
                    data.get("skip_reason"),
                )
        except Exception as exc:
            log.warning("db.write_window_prediction_failed", error=str(exc)[:120])

    async def update_window_prediction_outcome(self, window_ts: int, asset: str,
                                                oracle_winner: str) -> None:
        """After oracle resolution, update the prediction with actual outcome."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                _winner = oracle_winner.upper()
                await conn.execute("""
                    UPDATE window_predictions SET
                        oracle_winner = $4::varchar,
                        tiingo_correct = (tiingo_direction = $4::varchar),
                        chainlink_correct = (chainlink_direction = $4::varchar),
                        our_signal_correct = (our_signal_direction = $4::varchar)
                    WHERE window_ts = $1 AND asset = $2 AND timeframe = $3
                """, window_ts, asset, "5m", oracle_winner.upper())
        except Exception as exc:
            log.warning("db.update_prediction_outcome_failed", error=str(exc)[:120])

    async def store_post_resolution_analysis(self, result: dict) -> None:
        """
        Persist post-resolution AI analysis to DB.

        Writes to post_resolution_analyses table and also updates window_snapshots
        with summary columns for quick dashboard access.
        """
        if not self._pool:
            return
        try:
            window_ts = int(result["window_ts"])
            asset = result.get("asset", "BTC")
            timeframe = result.get("timeframe", "5m")
            oracle_direction = result.get("oracle_direction")
            n_ticks = int(result.get("n_ticks", 0))
            missed_profit = float(result.get("missed_profit_usd", 0.0))
            blocked_loss = float(result.get("blocked_loss_usd", 0.0))
            cap_too_tight = bool(result.get("cap_too_tight", False))
            gate_rec = result.get("gate_recommendation")
            ai_text = result.get("ai_post_analysis", "")

            async with self._pool.acquire() as conn:
                # Upsert into post_resolution_analyses
                await conn.execute(
                    """
                    INSERT INTO post_resolution_analyses (
                        window_ts, asset, timeframe,
                        oracle_direction, n_ticks,
                        missed_profit_usd, blocked_loss_usd,
                        cap_too_tight, gate_recommendation, ai_post_analysis
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (window_ts, asset, timeframe) DO UPDATE SET
                        oracle_direction  = EXCLUDED.oracle_direction,
                        n_ticks           = EXCLUDED.n_ticks,
                        missed_profit_usd = EXCLUDED.missed_profit_usd,
                        blocked_loss_usd  = EXCLUDED.blocked_loss_usd,
                        cap_too_tight     = EXCLUDED.cap_too_tight,
                        gate_recommendation = EXCLUDED.gate_recommendation,
                        ai_post_analysis  = EXCLUDED.ai_post_analysis,
                        analysed_at       = NOW()
                    """,
                    window_ts, asset, timeframe,
                    oracle_direction, n_ticks,
                    missed_profit, blocked_loss,
                    cap_too_tight, gate_rec, ai_text[:4000] if ai_text else None,
                )
                # Update summary columns in window_snapshots
                await conn.execute(
                    """
                    UPDATE window_snapshots
                    SET ai_post_analysis  = $1,
                        missed_profit_usd = $2,
                        blocked_loss_usd  = $3,
                        cap_too_tight     = $4,
                        gate_recommendation = $5
                    WHERE window_ts = $6 AND asset = $7 AND timeframe = $8
                    """,
                    ai_text[:4000] if ai_text else None,
                    missed_profit,
                    blocked_loss,
                    cap_too_tight,
                    gate_rec,
                    window_ts, asset, timeframe,
                )
            log.debug(
                "db.post_resolution_stored",
                window_ts=window_ts,
                missed=f"+${missed_profit:.2f}",
                avoided=f"-${blocked_loss:.2f}",
                cap_too_tight=cap_too_tight,
            )
        except Exception as exc:
            log.warning("db.store_post_resolution_failed", error=str(exc)[:120])

    async def get_eval_ticks_for_window(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
    ) -> list:
        """
        Fetch all evaluation ticks for a window from gate_audit table.
        Falls back to constructing from window_eval_history if gate_audit is empty.
        """
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        eval_offset        AS offset,
                        skip_reason,
                        vpin,
                        delta_pct,
                        regime,
                        gate_failed,
                        gate_passed,
                        decision
                    FROM gate_audit
                    WHERE window_ts = $1
                      AND asset     = $2
                      AND timeframe = $3
                    ORDER BY eval_offset DESC NULLS LAST
                    """,
                    window_ts, asset, timeframe,
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.debug("db.get_eval_ticks_failed", error=str(exc)[:80])
            return []
