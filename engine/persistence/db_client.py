"""
Database Client — Async PostgreSQL writes via asyncpg.

Handles all persistence for the trading engine:
  - Trade records (placed, resolved, PnL)
  - Signal snapshots (VPIN, cascade, arb)
  - System state (heartbeat, kill-switch status, bankroll, feed connectivity)

Schema reference: hub/db/schema.sql
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
import asyncpg
import structlog

from config.settings import Settings
from domain.entities import Order

log = structlog.get_logger(__name__)


# LT-04: channel name used for PostgreSQL LISTEN/NOTIFY between the
# hub (which INSERTs manual_trades rows) and the engine (which executes
# them). Keep in sync with hub/api/v58_monitor.py::post_manual_trade.
MANUAL_TRADE_NOTIFY_CHANNEL = "manual_trade_pending"


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
        # LT-04: dedicated pinned connection for LISTEN. asyncpg requires a
        # connection that is NOT shared with the pool because LISTEN holds
        # the connection open and callbacks fire on its read loop.
        self._listen_conn: Optional[asyncpg.Connection] = None
        self._listen_callback: Optional[Callable] = None
        self._listen_channel: Optional[str] = None

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
        # LT-04: release the pinned LISTEN connection first.
        try:
            await self.stop_listening()
        except Exception as exc:
            log.warning("db.stop_listening_on_close_failed", error=str(exc))
        if self._pool:
            await self._pool.close()
            log.info("db.closed")

    def _assert_pool(self) -> None:
        assert self._pool, "DBClient not connected — call connect() first"

    # ─── LT-04: PostgreSQL LISTEN/NOTIFY fast path ───────────────────────────
    #
    # The engine subscribes to the 'manual_trade_pending' channel over a
    # dedicated asyncpg connection (NOT from the pool — LISTEN holds the
    # connection open for the lifetime of the subscription). The hub
    # (hub/api/v58_monitor.py::post_manual_trade) emits a NOTIFY after it
    # INSERTs a row with status='pending_live', which wakes the engine's
    # manual_trade_poller immediately instead of making it wait for the
    # next 1s poll tick.
    #
    # Failure modes and safety:
    #   - If the LISTEN connection dies, the poll loop still fires every
    #     1s (safety-net fall-through). We also attempt reconnection on
    #     every fall-through tick via _ensure_listening().
    #   - If the hub fails to NOTIFY (e.g. the session.execute errors),
    #     the row is still in the DB and the 1s poll picks it up.
    #   - NOTIFY is transactional: the hub emits pg_notify AFTER the
    #     INSERT commit returns, so the row is guaranteed to be visible
    #     to the engine's SELECT by the time the notification is
    #     delivered.
    #
    # Reference:
    #   PostgreSQL LISTEN/NOTIFY: https://www.postgresql.org/docs/current/sql-listen.html
    #   asyncpg add_listener: https://magicstack.github.io/asyncpg/current/api/index.html#asyncpg.connection.Connection.add_listener

    async def listen(
        self,
        channel: str,
        callback: Callable[[asyncpg.Connection, int, str, str], Any],
    ) -> None:
        """Open a dedicated asyncpg connection and LISTEN on the given channel.

        The callback fires on the connection's read loop whenever a
        NOTIFY lands. Callback signature matches asyncpg's add_listener
        contract: (conn, pid, channel, payload).

        Safe to call multiple times — if already listening on a different
        channel, the previous listener is stopped first.
        """
        if self._listen_conn is not None:
            await self.stop_listening()

        try:
            # LT-04: dedicated connection, NOT from the pool. asyncpg
            # pools multiplex commands across connections and the LISTEN
            # state would be lost if the pool reused the connection for
            # other queries.
            self._listen_conn = await asyncpg.connect(dsn=self._dsn)
            self._listen_callback = callback
            self._listen_channel = channel
            await self._listen_conn.add_listener(channel, callback)
            log.info("db.listen_started", channel=channel)
        except Exception as exc:
            log.error("db.listen_failed", channel=channel, error=str(exc))
            # Cleanup partial state so _ensure_listening can retry.
            if self._listen_conn is not None:
                try:
                    await self._listen_conn.close()
                except Exception:
                    pass
            self._listen_conn = None
            self._listen_callback = None
            self._listen_channel = None
            raise

    async def stop_listening(self) -> None:
        """Release the pinned LISTEN connection. Safe to call if not listening."""
        if self._listen_conn is None:
            return
        try:
            if self._listen_callback and self._listen_channel:
                try:
                    await self._listen_conn.remove_listener(
                        self._listen_channel,
                        self._listen_callback,
                    )
                except Exception as exc:
                    log.debug("db.remove_listener_failed", error=str(exc))
            await self._listen_conn.close()
            log.info("db.listen_stopped", channel=self._listen_channel)
        finally:
            self._listen_conn = None
            self._listen_callback = None
            self._listen_channel = None

    def is_listening(self) -> bool:
        """Return True iff the pinned LISTEN connection is open and live."""
        if self._listen_conn is None:
            return False
        try:
            return not self._listen_conn.is_closed()
        except Exception:
            return False

    async def ensure_listening(
        self,
        channel: str,
        callback: Callable[[asyncpg.Connection, int, str, str], Any],
    ) -> bool:
        """Reconnect the LISTEN connection if it has died.

        Returns True if the listener is live after this call, False
        otherwise. Called from the poller's fall-through path so that
        a dropped connection is automatically re-established on the
        next poll tick.
        """
        if self.is_listening() and self._listen_channel == channel:
            return True
        try:
            await self.listen(channel, callback)
            return True
        except Exception as exc:
            log.warning(
                "db.ensure_listening_failed",
                channel=channel,
                error=str(exc)[:200],
            )
            return False

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
                engine_version, clob_order_id, fill_price, fill_size, execution_mode,
                strategy_id, strategy_version
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                      $18,$19,$20,$21,$22,$23,$24)
            ON CONFLICT (order_id) DO UPDATE SET
                status           = EXCLUDED.status,
                outcome          = EXCLUDED.outcome,
                payout_usd       = EXCLUDED.payout_usd,
                pnl_usd          = EXCLUDED.pnl_usd,
                resolved_at      = EXCLUDED.resolved_at,
                is_live          = EXCLUDED.is_live,
                entry_price      = EXCLUDED.entry_price,
                stake_usd        = EXCLUDED.stake_usd,
                metadata         = EXCLUDED.metadata,
                clob_order_id    = COALESCE(EXCLUDED.clob_order_id, trades.clob_order_id),
                fill_price       = COALESCE(EXCLUDED.fill_price, trades.fill_price),
                fill_size        = COALESCE(EXCLUDED.fill_size, trades.fill_size),
                execution_mode   = COALESCE(EXCLUDED.execution_mode, trades.execution_mode),
                strategy_id      = COALESCE(EXCLUDED.strategy_id, trades.strategy_id),
                strategy_version = COALESCE(EXCLUDED.strategy_version, trades.strategy_version)
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
            # strategy_id / strategy_version: prefer explicit metadata fields,
            # fall back to order.strategy (which is always set to strategy_id
            # by DBTradeRecorder).
            strategy_id = meta.get("strategy_id") or order.strategy or None
            strategy_version = meta.get("strategy_version") or None

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
                    # strategy identity fields
                    strategy_id,
                    strategy_version,
                )
            log.debug("db.trade_written", order_id=order.order_id)
        except Exception as exc:
            log.error("db.write_trade_failed", order_id=order.order_id, error=str(exc))
            raise

    # Alias for backward compat
    async def save_trade(self, order: Order) -> None:
        """Alias for write_trade (used by OrderManager)."""
        await self.write_trade(order)

    # ─── Window Dedup Queries ────────────────────────────────────────────────

    async def load_recent_traded_windows(self, hours: int = 2) -> set[str]:
        """
        Load recently traded window keys from the trades table.

        Returns a set of "{asset}-{window_ts}" strings for trades placed
        within the last `hours` hours.  Used by FiveMinVPINStrategy to
        restore dedup state after an engine restart.
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

    async def update_gamma_prices(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        gamma_up: float,
        gamma_down: float,
    ) -> None:
        """Store fresh T-60 Gamma prices to window_snapshot."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE window_snapshots SET gamma_up_price = $1, gamma_down_price = $2 WHERE window_ts = $3 AND asset = $4 AND timeframe = $5",
                    gamma_up,
                    gamma_down,
                    window_ts,
                    asset,
                    timeframe,
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
                    return {
                        "paper_enabled": row["paper_enabled"],
                        "live_enabled": row["live_enabled"],
                    }
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
            INSERT INTO system_state (id, engine_status, {
            ", ".join(c.split(" = ")[0] for c in updates)
        })
            VALUES (1, 'running', {", ".join(f"${i + 1}" for i in range(len(params)))})
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
                    redeem_request_type TEXT DEFAULT 'all',
                    quota_used_today INTEGER DEFAULT 0,
                    quota_limit INTEGER DEFAULT 100,
                    cooldown_until TIMESTAMPTZ,
                    cooldown_reason TEXT,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                INSERT INTO playwright_state (id) VALUES (1) ON CONFLICT DO NOTHING;
            """)
            await conn.execute("""
                ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS redeem_request_type TEXT DEFAULT 'all';
                ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS quota_used_today INTEGER DEFAULT 0;
                ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS quota_limit INTEGER DEFAULT 100;
                ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS cooldown_until TIMESTAMPTZ;
                ALTER TABLE playwright_state ADD COLUMN IF NOT EXISTS cooldown_reason TEXT;
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS redeem_events (
                    id SERIAL PRIMARY KEY,
                    redeem_type TEXT DEFAULT 'all',
                    redeemed_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    total_value DOUBLE PRECISION DEFAULT 0,
                    details_json JSONB DEFAULT '[]'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            await conn.execute("""
                ALTER TABLE redeem_events ADD COLUMN IF NOT EXISTS redeem_type TEXT DEFAULT 'all';
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
        quota_used_today: Optional[int] = None,
        quota_limit: Optional[int] = None,
        cooldown_until: Optional[datetime] = None,
        cooldown_reason: Optional[str] = None,
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
                            quota_used_today = COALESCE($9, quota_used_today),
                            quota_limit = COALESCE($10, quota_limit),
                            cooldown_until = COALESCE($11, cooldown_until),
                            cooldown_reason = COALESCE($12, cooldown_reason),
                            updated_at = NOW()
                        WHERE id = 1
                        """,
                        logged_in,
                        browser_alive,
                        usdc_balance,
                        positions_value,
                        json.dumps(positions_json or []),
                        json.dumps(redeemable_json or []),
                        json.dumps(history_json or []),
                        screenshot_png,
                        quota_used_today,
                        quota_limit,
                        cooldown_until,
                        cooldown_reason,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE playwright_state SET
                            logged_in = $1, browser_alive = $2,
                            usdc_balance = $3, positions_value = $4,
                            positions_json = $5, redeemable_json = $6,
                            history_json = $7,
                            quota_used_today = COALESCE($8, quota_used_today),
                            quota_limit = COALESCE($9, quota_limit),
                            cooldown_until = COALESCE($10, cooldown_until),
                            cooldown_reason = COALESCE($11, cooldown_reason),
                            updated_at = NOW()
                        WHERE id = 1
                        """,
                        logged_in,
                        browser_alive,
                        usdc_balance,
                        positions_value,
                        json.dumps(positions_json or []),
                        json.dumps(redeemable_json or []),
                        json.dumps(history_json or []),
                        quota_used_today,
                        quota_limit,
                        cooldown_until,
                        cooldown_reason,
                    )
        except Exception as e:
            log.error("db.playwright_state.error", error=str(e))

    async def request_redeem(self, redeem_type: str = "all") -> None:
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE playwright_state SET redeem_requested = TRUE, redeem_request_type = $1, updated_at = NOW() WHERE id = 1",
                    redeem_type,
                )
        except Exception as e:
            log.error("db.request_redeem.error", error=str(e))

    async def pop_redeem_request(self) -> Optional[str]:
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT redeem_requested, redeem_request_type FROM playwright_state WHERE id = 1"
                )
                if row and row["redeem_requested"]:
                    redeem_type = row["redeem_request_type"] or "all"
                    await conn.execute(
                        "UPDATE playwright_state SET redeem_requested = FALSE, redeem_request_type = 'all' WHERE id = 1"
                    )
                    return redeem_type
        except Exception as e:
            log.error("db.pop_redeem_request.error", error=str(e))
        return None

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
                    INSERT INTO redeem_events (redeem_type, redeemed_count, failed_count, total_value, details_json)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    result.get("redeem_type", "all"),
                    result.get("redeemed", 0),
                    result.get("failed", 0),
                    result.get("total_value", 0.0),
                    json.dumps(result.get("details", [])),
                )
        except Exception as e:
            log.error("db.redeem_event.error", error=str(e))

    async def get_redeem_quota_usage_today(self) -> int:
        if not self._pool:
            return 0
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(redeemed_count + failed_count), 0)
                    FROM redeem_events
                    WHERE created_at >= date_trunc('day', NOW())
                    """
                )
                return int(row or 0)
        except Exception as e:
            log.error("db.redeem_quota_usage.error", error=str(e))
            return 0

    async def get_latest_redeem_event(self) -> Optional[dict]:
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT redeem_type, redeemed_count, failed_count, total_value, details_json, created_at FROM redeem_events ORDER BY created_at DESC LIMIT 1"
                )
                return dict(row) if row else None
        except Exception as e:
            log.error("db.latest_redeem_event.error", error=str(e))
            return None

    # ─── Position Snapshot Tables (Hub /api/positions/snapshot) ──────────────
    # Tables: poly_pending_wins, redeemer_state
    # Migration: hub/db/migrations/versions/20260416_01_pending_wins.sql

    async def upsert_pending_wins(
        self,
        wins: list[dict],
        scan_successful: bool = True,
    ) -> None:
        """Replace the pending_wins set with the supplied list (atomic).

        ``wins`` items follow the schema returned by
        ``PositionRedeemer.pending_wins_summary()``:
            {"condition_id": str, "value": float,
             "window_end_utc": str|None, "overdue_seconds": int}

        Items with ``window_end_utc=None`` are skipped (column is NOT NULL).

        **Audit #204 (scan_successful guard):** when the upstream scan
        failed (caller passes ``scan_successful=False``), we SKIP the
        DELETE+INSERT entirely — an empty list from a failed scan is
        "unknown", NOT "no pending wins". Without this guard, a single
        transient data-api blip would wipe the Hub snapshot to
        ``0 pending`` even though positions are still on-chain —
        observed in prod 2026-04-16: Wallet $83.31 · Pending $112.71
        (14 pending) wiped to 0 in 60s with zero on-chain activity.
        """
        if not self._pool:
            return

        if not scan_successful:
            # Preserve existing DB snapshot. Warn loud so ops know the
            # POSITION SNAPSHOT may be stale. Cheap — one log line per
            # failed scan (scan runs every 15 min).
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT COUNT(*) AS c FROM poly_pending_wins"
                    )
                    existing = int((row or {}).get("c", 0))
            except Exception:
                existing = -1
            log.warning(
                "db.upsert_pending_wins.skipped_due_to_scan_failure",
                existing_rows=existing,
                reason="preserving prior snapshot — upstream scan unsuccessful",
            )
            return

        rows: list[tuple] = []
        for w in wins or []:
            wend = w.get("window_end_utc")
            if not wend:
                continue
            # Coerce ISO-8601 string → datetime so asyncpg has a stable cast.
            if isinstance(wend, str):
                # datetime.fromisoformat() in Py3.11+ handles trailing 'Z'.
                try:
                    wend_dt = datetime.fromisoformat(wend.replace("Z", "+00:00"))
                except ValueError:
                    continue
            else:
                wend_dt = wend
            rows.append((str(w["condition_id"]), float(w.get("value") or 0.0), wend_dt))

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM poly_pending_wins")
                if rows:
                    await conn.executemany(
                        "INSERT INTO poly_pending_wins (condition_id, value, window_end_utc) "
                        "VALUES ($1, $2, $3)",
                        rows,
                    )

    async def upsert_redeemer_state(
        self,
        cooldown: dict,
        daily_quota_limit: int,
        quota_used_today: int,
    ) -> None:
        """Append a redeemer_state row capturing the current cooldown + quota.

        Task #196 also records backoff visibility (``backoff_active``,
        ``backoff_remaining_seconds``, ``consecutive_429_count``) when those
        keys are present on ``cooldown``. Missing keys default to 0/False,
        preserving backward compatibility with older callers that never
        set them.
        """
        if not self._pool:
            return
        resets_at = cooldown.get("resets_at") if cooldown else None
        if isinstance(resets_at, str):
            try:
                resets_at = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
            except ValueError:
                resets_at = None
        cd = cooldown or {}
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO redeemer_state "
                "(cooldown_active, cooldown_remaining_seconds, cooldown_resets_at, "
                " cooldown_reason, daily_quota_limit, quota_used_today, "
                " backoff_active, backoff_remaining_seconds, consecutive_429_count) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                bool(cd.get("active")),
                int(cd.get("remaining_seconds") or 0),
                resets_at,
                cd.get("reason") or "",
                int(daily_quota_limit),
                int(quota_used_today),
                bool(cd.get("backoff_active")),
                int(cd.get("backoff_remaining_seconds") or 0),
                int(cd.get("consecutive_429_count") or 0),
            )

    async def count_redeems_today(self) -> int:
        """Count redeem ATTEMPT rows from start of today UTC.

        Reads from ``redeem_attempts`` (see migrations/add_redeem_attempts_table.sql).
        Returns 0 if the pool is not connected or the query fails.
        """
        if not self._pool:
            return 0
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS c FROM redeem_attempts "
                    "WHERE attempted_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
                )
                return int(row["c"] or 0) if row else 0
        except Exception as e:
            log.warning("db.count_redeems_today.error", error=str(e)[:120])
            return 0

    async def count_redeems_last_hour(self) -> int:
        """Count redeem ATTEMPT rows from the last rolling 60 min.

        Used by the per-hour throttle (Daisy-set 2026-04-16: default
        4/hr) to gate the auto-sweep between the 15-min ticks so we
        never burn >4 wins per hour without explicit operator intent.
        Rolling window, not calendar-hour — deterministic across restarts.
        """
        if not self._pool:
            return 0
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS c FROM redeem_attempts "
                    "WHERE attempted_at >= NOW() - INTERVAL '1 hour'"
                )
                return int(row["c"] or 0) if row else 0
        except Exception as e:
            log.warning("db.count_redeems_last_hour.error", error=str(e)[:120])
            return 0

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
                        await conn.execute(
                            f"ALTER TABLE window_snapshots ADD COLUMN IF NOT EXISTS {col} {col_type}"
                        )
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

    async def update_window_surface_fields(
        self,
        *,
        window_ts: int,
        asset: str,
        timeframe: str,
        eval_offset: Optional[int],
        surface_fields: dict,
    ) -> None:
        """Upsert v3/v4 surface columns on an existing window_snapshots row.

        v4.4.0 (2026-04-16): called from StrategyRegistry._write_window_trace
        after every window evaluation. Populates the 17 v3/v4 columns that
        are defined in the schema but were never written by the legacy
        ``write_window_snapshot`` path (legacy path doesn't have a
        FullDataSurface handle, registry does).

        Fire-and-forget: never raises. INSERT ... ON CONFLICT upsert so it
        creates a minimal row if the legacy writer hasn't run yet.
        """
        if not self._pool:
            return
        if eval_offset is None:
            eval_offset = 0
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO window_snapshots (
                        window_ts, asset, timeframe, eval_offset,
                        sub_signal_elm, sub_signal_cascade, sub_signal_taker,
                        sub_signal_vpin, sub_signal_momentum,
                        sub_signal_oi, sub_signal_funding,
                        regime_confidence, regime_persistence,
                        strategy_conviction, strategy_conviction_score,
                        consensus_safe_to_trade, consensus_agreement_score,
                        consensus_divergence_bps,
                        macro_bias, macro_direction_gate, macro_size_modifier
                    ) VALUES (
                        $1,$2,$3,$4,
                        $5,$6,$7,$8,$9,$10,$11,
                        $12,$13,$14,$15,
                        $16,$17,$18,
                        $19,$20,$21
                    )
                    ON CONFLICT (window_ts, asset, timeframe, eval_offset) DO UPDATE SET
                        sub_signal_elm         = COALESCE(EXCLUDED.sub_signal_elm, window_snapshots.sub_signal_elm),
                        sub_signal_cascade     = COALESCE(EXCLUDED.sub_signal_cascade, window_snapshots.sub_signal_cascade),
                        sub_signal_taker       = COALESCE(EXCLUDED.sub_signal_taker, window_snapshots.sub_signal_taker),
                        sub_signal_vpin        = COALESCE(EXCLUDED.sub_signal_vpin, window_snapshots.sub_signal_vpin),
                        sub_signal_momentum    = COALESCE(EXCLUDED.sub_signal_momentum, window_snapshots.sub_signal_momentum),
                        sub_signal_oi          = COALESCE(EXCLUDED.sub_signal_oi, window_snapshots.sub_signal_oi),
                        sub_signal_funding     = COALESCE(EXCLUDED.sub_signal_funding, window_snapshots.sub_signal_funding),
                        regime_confidence      = COALESCE(EXCLUDED.regime_confidence, window_snapshots.regime_confidence),
                        regime_persistence     = COALESCE(EXCLUDED.regime_persistence, window_snapshots.regime_persistence),
                        strategy_conviction    = COALESCE(EXCLUDED.strategy_conviction, window_snapshots.strategy_conviction),
                        strategy_conviction_score = COALESCE(EXCLUDED.strategy_conviction_score, window_snapshots.strategy_conviction_score),
                        consensus_safe_to_trade = COALESCE(EXCLUDED.consensus_safe_to_trade, window_snapshots.consensus_safe_to_trade),
                        consensus_agreement_score = COALESCE(EXCLUDED.consensus_agreement_score, window_snapshots.consensus_agreement_score),
                        consensus_divergence_bps = COALESCE(EXCLUDED.consensus_divergence_bps, window_snapshots.consensus_divergence_bps),
                        macro_bias             = COALESCE(EXCLUDED.macro_bias, window_snapshots.macro_bias),
                        macro_direction_gate   = COALESCE(EXCLUDED.macro_direction_gate, window_snapshots.macro_direction_gate),
                        macro_size_modifier    = COALESCE(EXCLUDED.macro_size_modifier, window_snapshots.macro_size_modifier)
                    """,
                    int(window_ts),
                    asset,
                    timeframe,
                    int(eval_offset),
                    surface_fields.get("sub_signal_elm"),
                    surface_fields.get("sub_signal_cascade"),
                    surface_fields.get("sub_signal_taker"),
                    surface_fields.get("sub_signal_vpin"),
                    surface_fields.get("sub_signal_momentum"),
                    surface_fields.get("sub_signal_oi"),
                    surface_fields.get("sub_signal_funding"),
                    surface_fields.get("regime_confidence"),
                    surface_fields.get("regime_persistence"),
                    surface_fields.get("strategy_conviction"),
                    surface_fields.get("strategy_conviction_score"),
                    surface_fields.get("consensus_safe_to_trade"),
                    surface_fields.get("consensus_agreement_score"),
                    surface_fields.get("consensus_divergence_bps"),
                    surface_fields.get("macro_bias"),
                    surface_fields.get("macro_direction_gate"),
                    surface_fields.get("macro_size_modifier"),
                )
        except Exception as exc:
            log.warning(
                "db.update_window_surface_fields_failed",
                error=str(exc)[:160],
                asset=asset,
                window_ts=window_ts,
            )

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
            "chainlink_open",
            "chainlink_close",
            "tiingo_open",
            "tiingo_close",
            "poly_resolved_outcome",
            "poly_up_price_final",
            "poly_down_price_final",
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
                    window_ts,
                    asset,
                    timeframe,
                    *params,
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
                    window_ts,
                    asset,
                    timeframe,
                    *params,
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
                        "macro_reasoning": row["reasoning"][:100]
                        if row["reasoning"]
                        else "",
                    }
                return None
        except Exception:
            return None

    async def get_latest_clob_prices(
        self, asset: str = "BTC", max_age_seconds: int = 30
    ) -> dict | None:
        """Get the most recent CLOB book prices for an asset.

        Args:
            asset: Asset symbol (default: "BTC")
            max_age_seconds: Reject rows older than this many seconds (default: 30).
                             Prevents stale pre-restart rows from being used as
                             entry caps after an engine restart.
        """
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT up_best_bid, up_best_ask, down_best_bid, down_best_ask "
                    "FROM ticks_clob WHERE asset = $1 "
                    "  AND ts > NOW() - ($2 || ' seconds')::interval "
                    "ORDER BY ts DESC LIMIT 1",
                    asset,
                    str(max_age_seconds),
                )
                if row:
                    return {
                        "clob_up_bid": float(row["up_best_bid"])
                        if row["up_best_bid"]
                        else None,
                        "clob_up_ask": float(row["up_best_ask"])
                        if row["up_best_ask"]
                        else None,
                        "clob_down_bid": float(row["down_best_bid"])
                        if row["down_best_bid"]
                        else None,
                        "clob_down_ask": float(row["down_best_ask"])
                        if row["down_best_ask"]
                        else None,
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
        """Mark a trade as EXPIRED in the DB (used by startup reconciliation).

        Safety: refuses to expire trades that have confirmed CLOB fills
        (clob_status=MATCHED or shares_filled > 0). Those are real positions
        that the orphan reconciler will resolve.
        """
        if not self._pool:
            return
        try:
            from datetime import timezone

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

    async def get_oracle_outcome_for_trade(self, trade_row: dict) -> Optional[str]:
        """Look up oracle outcome for a paper trade from window_snapshots.

        Returns "UP" or "DOWN" if the window has resolved, None otherwise.
        Used by startup reconciliation to record paper trade WIN/LOSS.
        """
        if not self._pool:
            return None
        try:
            # Try to get window_ts from the trade metadata or related fields
            meta = trade_row.get("metadata") or {}
            if isinstance(meta, str):
                import json as _j

                try:
                    meta = _j.loads(meta)
                except Exception:
                    meta = {}

            window_ts = (
                meta.get("window_ts")
                or trade_row.get("window_ts")
                or trade_row.get("created_at")
            )
            asset = trade_row.get("asset", "BTC")

            if not window_ts:
                return None

            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT actual_direction FROM window_snapshots
                       WHERE window_ts::text LIKE $1 AND asset = $2
                         AND actual_direction IS NOT NULL
                       LIMIT 1""",
                    f"{str(window_ts)[:10]}%",
                    asset,
                )
                return row["actual_direction"] if row else None
        except Exception as exc:
            log.warning("db.get_oracle_outcome_failed", error=str(exc)[:100])
            return None

    async def resolve_paper_trade(
        self,
        order_id: str,
        outcome: str,
        pnl_usd: float,
        resolved_direction: str,
    ) -> None:
        """Resolve a paper trade with WIN/LOSS outcome from oracle data.

        Used by startup reconciliation to properly record paper trade results
        instead of just expiring them — preserving W/L data for strategy analysis.
        """
        if not self._pool:
            return
        try:
            from datetime import timezone

            async with self._pool.acquire() as conn:
                await conn.execute(
                    """UPDATE trades
                       SET status = $1,
                           outcome = $2,
                           pnl_usd = $3,
                           resolved_at = NOW() + (id * INTERVAL '1 millisecond'),
                           metadata = jsonb_set(
                               COALESCE(metadata::jsonb, '{}'::jsonb),
                               '{resolved_direction}', $4::jsonb
                           )
                       WHERE order_id = $5 AND status = 'OPEN'""",
                    f"RESOLVED_{outcome}",
                    outcome,
                    pnl_usd,
                    f'"{resolved_direction}"',
                    order_id,
                )
            log.info(
                "db.paper_trade_resolved",
                order_id=order_id[:32],
                outcome=outcome,
                pnl_usd=round(pnl_usd, 2),
            )
        except Exception as exc:
            log.error(
                "db.paper_trade_resolve_failed",
                order_id=order_id[:32],
                error=str(exc)[:100],
            )

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

    async def get_token_ids_from_market_data(
        self,
        asset: str,
        window_ts: int,
        timeframe: str = "5m",
    ) -> dict | None:
        """LT-02: fallback lookup for CLOB token_ids when the in-memory
        ring buffer in FiveMinVPINStrategy._recent_windows doesn't have
        the window (e.g., engine just restarted, or the user clicked
        manual trade on a stale window that aged out of the buffer).

        Queries the market_data table which is UPSERTed per window by the
        data-collector service on the same Montreal box. Returns a dict
        with keys up_token_id and down_token_id, or None if the row
        doesn't exist or both token_ids are NULL.

        Bidirectional tolerance (±60s) on the window_ts match because
        the engine's window_ts is the window-close epoch while the
        data-collector may UPSERT on the window-open or mid epoch.
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

        POLY-SOT (2026-04-11): when ``clob_order_id`` is provided we now also
        persist it to the new ``polymarket_order_id`` column so the SOT
        reconciler loop can later query Polymarket and stamp the
        ``polymarket_confirmed_*`` / ``sot_reconciliation_state`` columns.
        Caller doesn't have to know about the SOT plumbing — it just passes
        the order ID it got back from ``poly_client.place_order(...)``.
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
                # Wrapped in its own UPDATE so a missing column on a stale
                # database (operator hasn't run the migration yet) silently
                # no-ops the column write but the status update above still
                # succeeds. The reconciler will catch missing IDs on its
                # next pass via the `unreconciled` path.
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

    # ─── POLY-SOT helpers ────────────────────────────────────────────────────

    async def ensure_manual_trades_sot_columns(self) -> None:
        """Add POLY-SOT columns to manual_trades if missing (idempotent).

        The hub also adds these columns at lifespan startup
        (hub/api/v58_monitor.py::ensure_manual_trades_table). The engine
        ensures them too because the engine restart cycle is independent
        from the hub's, and the SOT reconciler loop will fail loudly if it
        tries to write a column that doesn't exist yet.

        Mirrors the existing ``ensure_v8_trade_columns`` pattern in this
        same file.
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
                    # POLY-SOT-d: on-chain Polygon tx hash (from poly_fills).
                    # NULL until the reconciler matches this row to a
                    # poly_fills row. The tx hash is the cryptographic proof
                    # that the fill actually landed on-chain.
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
                # POLY-SOT-d: partial index on non-NULL tx hashes so the
                # audit path (show the on-chain proof for a specific trade)
                # stays cheap.
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
        """Return recent manual_trades rows that the SOT reconciler should
        re-verify against Polymarket.

        Returns rows where:
          * status indicates the engine *thinks* it executed (executed,
            executing, open, pending_live, pending_paper)
          * created_at is older than 30 seconds (the engine has had time to
            finish its own write)
          * AND either sot_reconciliation_state is NULL/unreconciled, OR
            it has been more than 5 minutes since the last verification

        The 30-second floor avoids racing against the manual_trade_poller
        which may still be inside its retry loop.
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
        """Stamp a manual_trades row with the latest SOT reconciliation result.

        Always bumps polymarket_last_verified_at = NOW() so the next pass can
        skip rows that were checked recently. Caller computes the
        sot_reconciliation_state — see CLOBReconciler.reconcile_manual_trades_sot
        for the decision matrix.
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

    # ─── POLY-SOT-b helpers for the `trades` table ───────────────────────────
    #
    # Mirror the manual_trades helpers above byte-for-byte except for the
    # table name. The reconciler imports both pairs and dispatches based on
    # which table it's walking on a given pass.

    async def ensure_trades_sot_columns(self) -> None:
        """Add POLY-SOT columns to the `trades` table if missing (idempotent).

        See migrations/add_trades_sot_columns.sql for the canonical migration.
        Engine ensures these on every startup so a stale DB converges to the
        full schema after one more lifespan cycle without operator action.
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
                    # POLY-SOT-d: on-chain Polygon tx hash (from poly_fills).
                    # See manual_trades ensure method for the full rationale.
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
                # POLY-SOT-d: partial index on non-NULL tx hashes.
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
        """Return recent automatic-trade rows that the SOT reconciler should
        re-verify against Polymarket.

        Mirrors fetch_manual_trades_for_sot_check but walks the `trades`
        table. The trades table uses `clob_order_id` as its existing CLOB ID
        field (added by the v8 migration); we COALESCE that into
        polymarket_order_id so a row that has the older field but not yet
        the new one is still picked up by the reconciler.

        Filters:
          * status indicates the engine *thinks* it executed (FILLED, OPEN,
            EXPIRED with shares_filled, MATCHED) — the same alphabet
            _resolve_orphaned_fills uses
          * created_at older than 30 seconds (engine has finished its write)
          * is_live = true so paper-mode rows don't drown the loop
          * sot_reconciliation_state is NULL/unreconciled/diverged/engine_optimistic
            OR last_verified_at is older than 5 minutes
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
        """Stamp a `trades` row with the latest SOT reconciliation result.

        Always bumps polymarket_last_verified_at = NOW() so the next pass can
        skip rows that were checked recently. The trade_id parameter is the
        integer primary key of the trades table (matches `trades.id`), in
        contrast to manual_trades which uses a string trade_id.
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

    async def get_window_close(
        self, window_ts: int, asset: str, timeframe: str
    ) -> float:
        """Get the close price for a resolved window from window_snapshots."""
        if not self._pool:
            return 0.0
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT close_price FROM window_snapshots WHERE window_ts = $1 AND asset = $2 AND timeframe = $3",
                    window_ts,
                    asset,
                    timeframe,
                )
                return float(row) if row else 0.0
        except Exception:
            return 0.0

    async def update_window_trade_placed(
        self, window_ts: int, asset: str, timeframe: str
    ) -> None:
        """Mark a window_snapshot as having a trade placed."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE window_snapshots SET trade_placed = TRUE WHERE window_ts = $1 AND asset = $2 AND timeframe = $3",
                    window_ts,
                    asset,
                    timeframe,
                )
                import structlog

                structlog.get_logger().info(
                    "db.trade_placed_updated",
                    window_ts=window_ts,
                    asset=asset,
                    result=result,
                )
        except Exception as exc:
            import structlog

            structlog.get_logger().error(
                "db.trade_placed_update_failed",
                window_ts=window_ts,
                asset=asset,
                error=str(exc),
            )

    async def update_window_fok_data(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        execution_mode: str,
        fok_attempts: int,
        fok_fill_step: int,
        clob_fill_price: float,
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
                    window_ts,
                    asset,
                    timeframe,
                    execution_mode,
                    fok_attempts,
                    fok_fill_step,
                    clob_fill_price,
                )
        except Exception as exc:
            import structlog

            structlog.get_logger().error(
                "db.fok_data_update_failed", window_ts=window_ts, error=str(exc)
            )

    async def update_window_skip_reason(
        self, window_ts: int, asset: str, timeframe: str, skip_reason: str
    ) -> None:
        """Update skip_reason on a window_snapshot after evaluation."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE window_snapshots SET skip_reason = $1 WHERE window_ts = $2 AND asset = $3 AND timeframe = $4",
                    skip_reason,
                    window_ts,
                    asset,
                    timeframe,
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
                    float(data.get("confidence", 0))
                    if data.get("confidence") is not None
                    else None,
                    bool(data.get("agreement"))
                    if data.get("agreement") is not None
                    else None,
                    data.get("action"),
                    data.get("notes"),
                    float(data.get("chainlink_price"))
                    if data.get("chainlink_price") is not None
                    else None,
                    float(data.get("tiingo_price"))
                    if data.get("tiingo_price") is not None
                    else None,
                    float(data.get("binance_price"))
                    if data.get("binance_price") is not None
                    else None,
                )
        except Exception as exc:
            log.debug("db.write_countdown_evaluation_failed", error=str(exc)[:120])

    async def write_evaluation(self, data: dict) -> None:
        """
        Write a Claude evaluation to countdown_evaluations (compatibility shim).
        Maps claude_evaluator's write_evaluation call to the countdown_evaluations table.
        """
        await self.write_countdown_evaluation(
            {
                "window_ts": int(data.get("timestamp", 0)),
                "stage": "claude_eval",
                "direction": data.get("direction"),
                "confidence": data.get("confidence"),
                "agreement": data.get("trade_placed"),
                "action": "TRADE" if data.get("trade_placed") else "SKIP",
                "notes": data.get("analysis", "")[:2000]
                if data.get("analysis")
                else None,
            }
        )

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
                    ("strategy_id", "VARCHAR(64)"),
                    ("strategy_version", "VARCHAR(32)"),
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
        """Retired — gate_audit superseded by gate_check_traces (feat/trace PR).

        This method is intentionally a no-op.  All gate-check persistence now goes
        through the WindowTraceRepository (pg_window_trace_repo) which writes to
        ``gate_check_traces``.  The legacy ``gate_audit`` table remains in the DB
        until the operator manually executes migrations/retire_gate_audit_table.sql.
        """
        return  # no-op

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
                    float(data["clob_up_bid"])
                    if data.get("clob_up_bid") is not None
                    else None,
                    float(data["clob_up_ask"])
                    if data.get("clob_up_ask") is not None
                    else None,
                    float(data["clob_down_bid"])
                    if data.get("clob_down_bid") is not None
                    else None,
                    float(data["clob_down_ask"])
                    if data.get("clob_down_ask") is not None
                    else None,
                    float(data["binance_price"])
                    if data.get("binance_price") is not None
                    else None,
                    float(data["tiingo_open"])
                    if data.get("tiingo_open") is not None
                    else None,
                    float(data["tiingo_close"])
                    if data.get("tiingo_close") is not None
                    else None,
                    float(data["chainlink_price"])
                    if data.get("chainlink_price") is not None
                    else None,
                    float(data["delta_pct"])
                    if data.get("delta_pct") is not None
                    else None,
                    float(data["delta_tiingo"])
                    if data.get("delta_tiingo") is not None
                    else None,
                    float(data["delta_binance"])
                    if data.get("delta_binance") is not None
                    else None,
                    float(data["delta_chainlink"])
                    if data.get("delta_chainlink") is not None
                    else None,
                    data.get("delta_source"),
                    float(data["vpin"]) if data.get("vpin") is not None else None,
                    data.get("regime"),
                    float(data["clob_spread"])
                    if data.get("clob_spread") is not None
                    else None,
                    float(data["clob_mid"])
                    if data.get("clob_mid") is not None
                    else None,
                    float(data["v2_probability_up"])
                    if data.get("v2_probability_up") is not None
                    else None,
                    data.get("v2_direction"),
                    bool(data["v2_agrees"])
                    if data.get("v2_agrees") is not None
                    else None,
                    bool(data["v2_high_conf"])
                    if data.get("v2_high_conf") is not None
                    else None,
                    data.get("v2_model_version"),
                    data.get(
                        "v2_quantiles"
                    ),  # JSONB (already serialized as JSON string)
                    data.get("v2_quantiles_at_close"),  # JSONB
                    bool(data["gate_vpin_passed"])
                    if data.get("gate_vpin_passed") is not None
                    else None,
                    bool(data["gate_delta_passed"])
                    if data.get("gate_delta_passed") is not None
                    else None,
                    bool(data["gate_cg_passed"])
                    if data.get("gate_cg_passed") is not None
                    else None,
                    bool(data["gate_twap_passed"])
                    if data.get("gate_twap_passed") is not None
                    else None,
                    bool(data["gate_timesfm_passed"])
                    if data.get("gate_timesfm_passed") is not None
                    else None,
                    bool(data.get("gate_passed", False)),
                    data.get("gate_failed"),
                    data.get("decision", "SKIP"),
                    float(data["twap_delta"])
                    if data.get("twap_delta") is not None
                    else None,
                    data.get("twap_direction"),
                    bool(data["twap_gamma_agree"])
                    if data.get("twap_gamma_agree") is not None
                    else None,
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
                await conn.execute(
                    """
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

    async def update_window_prediction_outcome(
        self, window_ts: int, asset: str, oracle_winner: str
    ) -> None:
        """After oracle resolution, update the prediction with actual outcome."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                _winner = oracle_winner.upper()
                await conn.execute(
                    """
                    UPDATE window_predictions SET
                        oracle_winner = $4::varchar,
                        tiingo_correct = (tiingo_direction = $4::varchar),
                        chainlink_correct = (chainlink_direction = $4::varchar),
                        our_signal_correct = (our_signal_direction = $4::varchar)
                    WHERE window_ts = $1 AND asset = $2 AND timeframe = $3
                """,
                    window_ts,
                    asset,
                    "5m",
                    oracle_winner.upper(),
                )
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
                    window_ts,
                    asset,
                    timeframe,
                    oracle_direction,
                    n_ticks,
                    missed_profit,
                    blocked_loss,
                    cap_too_tight,
                    gate_rec,
                    ai_text[:4000] if ai_text else None,
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
                    window_ts,
                    asset,
                    timeframe,
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
        """Fetch all evaluation ticks for a window from gate_check_traces.

        Supersedes the legacy gate_audit read path.  Returns one dict per
        (eval_offset, gate_order) row, shaped to match the downstream
        post_resolution_evaluator contract.
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
                        passed             AS gate_passed,
                        passed             AS gate_passed,
                        reason             AS gate_failed,
                        action             AS decision,
                        observed_json->>'vpin'      AS vpin,
                        observed_json->>'delta_pct' AS delta_pct,
                        observed_json->>'regime'    AS regime
                    FROM gate_check_traces
                    WHERE window_ts = $1
                      AND asset     = $2
                      AND timeframe = $3
                    ORDER BY eval_offset DESC NULLS LAST, gate_order ASC
                    """,
                    window_ts,
                    asset,
                    timeframe,
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.debug("db.get_eval_ticks_failed", error=str(exc)[:80])
            return []

    async def write_clob_execution_log(self, data: dict) -> None:
        """
        Log comprehensive CLOB execution data for every FOK attempt, GTC placement, fill, or kill.

        Captures: target price/size, CLOB state at execution, execution mode, ladder attempts,
        fill details, error messages, and latency.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO clob_execution_log (
                        asset, timeframe, window_ts, outcome, token_id,
                        direction, strategy, eval_offset,
                        target_price, target_size, max_price, min_price,
                        clob_best_ask, clob_best_bid,
                        execution_mode, fok_attempt_num, fok_max_attempts,
                        status, fill_price, fill_size, order_id,
                        error_code, error_message, latency_ms, metadata
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15, $16, $17, $18, $19, $20, $21,
                        $22, $23, $24, $25
                    )
                    ON CONFLICT (window_ts, outcome, ts, execution_mode, fok_attempt_num)
                    DO NOTHING
                    """,
                    data.get("asset", "BTC"),
                    data.get("timeframe", "5m"),
                    int(data.get("window_ts", 0)),
                    data.get("outcome", "UP"),
                    data.get("token_id"),
                    data.get("direction", "BUY"),
                    data.get("strategy"),
                    data.get("eval_offset"),
                    float(data["target_price"])
                    if data.get("target_price") is not None
                    else None,
                    float(data["target_size"])
                    if data.get("target_size") is not None
                    else None,
                    float(data["max_price"])
                    if data.get("max_price") is not None
                    else None,
                    float(data["min_price"])
                    if data.get("min_price") is not None
                    else None,
                    float(data["clob_best_ask"])
                    if data.get("clob_best_ask") is not None
                    else None,
                    float(data["clob_best_bid"])
                    if data.get("clob_best_bid") is not None
                    else None,
                    data.get("execution_mode", "FOK"),
                    data.get("fok_attempt_num"),
                    data.get("fok_max_attempts"),
                    data.get("status", "submitted"),
                    float(data["fill_price"])
                    if data.get("fill_price") is not None
                    else None,
                    float(data["fill_size"])
                    if data.get("fill_size") is not None
                    else None,
                    data.get("order_id"),
                    data.get("error_code"),
                    data.get("error_message"),
                    data.get("latency_ms"),
                    data.get("metadata", {}),
                )
        except Exception as exc:
            log.warning("db.write_clob_execution_log_failed", error=str(exc)[:200])

    async def write_fok_ladder_attempt(self, data: dict) -> None:
        """Log individual FOK ladder attempt within an execution."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO fok_ladder_attempts (
                        execution_log_id, attempt_num, attempt_price, attempt_size,
                        clob_best_ask, clob_best_bid,
                        status, fill_size, fill_price,
                        error_message, attempt_duration_ms
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
                    )
                    ON CONFLICT (execution_log_id, attempt_num) DO NOTHING
                    """,
                    data.get("execution_log_id"),
                    data.get("attempt_num"),
                    float(data["attempt_price"])
                    if data.get("attempt_price") is not None
                    else None,
                    float(data["attempt_size"])
                    if data.get("attempt_size") is not None
                    else None,
                    float(data["clob_best_ask"])
                    if data.get("clob_best_ask") is not None
                    else None,
                    float(data["clob_best_bid"])
                    if data.get("clob_best_bid") is not None
                    else None,
                    data.get("status", "attempted"),
                    float(data["fill_size"])
                    if data.get("fill_size") is not None
                    else None,
                    float(data["fill_price"])
                    if data.get("fill_price") is not None
                    else None,
                    data.get("error_message"),
                    data.get("attempt_duration_ms"),
                )
        except Exception as exc:
            log.warning("db.write_fok_ladder_attempt_failed", error=str(exc)[:200])

    async def write_clob_book_snapshot(self, data: dict) -> None:
        """Log complete CLOB book snapshot on every poll (not just during execution)."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO clob_book_snapshots (
                        asset, timeframe, window_ts,
                        up_token_id, down_token_id,
                        up_best_bid, up_best_ask, up_bid_depth, up_ask_depth,
                        down_best_bid, down_best_ask, down_bid_depth, down_ask_depth,
                        up_spread, down_spread, mid_price,
                        up_bids_top5, up_asks_top5, down_bids_top5, down_asks_top5
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                        $14, $15, $16, $17, $18, $19, $20
                    )
                    ON CONFLICT (window_ts, up_token_id, down_token_id, ts) DO NOTHING
                    """,
                    data.get("asset", "BTC"),
                    data.get("timeframe", "5m"),
                    int(data.get("window_ts", 0)),
                    data.get("up_token_id"),
                    data.get("down_token_id"),
                    float(data["up_best_bid"])
                    if data.get("up_best_bid") is not None
                    else None,
                    float(data["up_best_ask"])
                    if data.get("up_best_ask") is not None
                    else None,
                    float(data["up_bid_depth"])
                    if data.get("up_bid_depth") is not None
                    else None,
                    float(data["up_ask_depth"])
                    if data.get("up_ask_depth") is not None
                    else None,
                    float(data["down_best_bid"])
                    if data.get("down_best_bid") is not None
                    else None,
                    float(data["down_best_ask"])
                    if data.get("down_best_ask") is not None
                    else None,
                    float(data["down_bid_depth"])
                    if data.get("down_bid_depth") is not None
                    else None,
                    float(data["down_ask_depth"])
                    if data.get("down_ask_depth") is not None
                    else None,
                    float(data["up_spread"])
                    if data.get("up_spread") is not None
                    else None,
                    float(data["down_spread"])
                    if data.get("down_spread") is not None
                    else None,
                    float(data["mid_price"])
                    if data.get("mid_price") is not None
                    else None,
                    data.get("up_bids_top5", []),
                    data.get("up_asks_top5", []),
                    data.get("down_bids_top5", []),
                    data.get("down_asks_top5", []),
                )
        except Exception as exc:
            log.warning("db.write_clob_book_snapshot_failed", error=str(exc)[:200])

    async def fetch_recent_fills_for_condition(
        self,
        condition_id: Optional[str],
        within_seconds: int = 60,
    ) -> list[dict]:
        """Return poly_fills rows for `condition_id` matched in the last
        `within_seconds`, ordered oldest-first.

        Used by the multi-fill (FAK split) detector in the engine before it
        calls `TelegramAlerter.send_order_filled`. A FAK order can split
        across the layered ask book and produce multiple poly_fills rows
        for the same condition_id at the same timestamp; we want to surface
        each leg in the alert.

        Returns an empty list when condition_id is falsy, the pool isn't
        connected, or anything goes wrong — callers treat absence of fills
        as "no split block to render".
        """
        if not condition_id or not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT price, size, transaction_hash AS tx
                    FROM poly_fills
                    WHERE condition_id = $1
                      AND match_time_utc >= NOW() - make_interval(secs => $2)
                    ORDER BY match_time_utc ASC
                    """,
                    condition_id,
                    int(within_seconds),
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.warning(
                "db.fetch_recent_fills_for_condition_failed",
                error=str(exc)[:200],
                condition_id=condition_id,
            )
            return []


class DBClientLegacyShim:
    """Thin delegate to pg_*_repo adapters.

    Wraps the existing DBClient to provide the same interface while
    routing calls to the correct adapter. Delete once orchestrator is
    retired (Phase 5).

    The orchestrator accesses self._db._pool directly — this shim
    exposes it as a property for backward compat.
    """

    def __init__(self, db_client: "DBClient") -> None:
        self._inner = db_client

    @property
    def _pool(self):
        """Expose pool for backward compat — orchestrator passes this to other components."""
        return self._inner._pool

    async def connect(self) -> None:
        return await self._inner.connect()

    async def close(self) -> None:
        return await self._inner.close()

    def __getattr__(self, name: str):
        """Fall through to inner DBClient for any method not explicitly delegated."""
        return getattr(self._inner, name)
