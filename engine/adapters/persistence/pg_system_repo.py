"""PostgreSQL System Repository -- per-aggregate persistence for system state.

Handles system_state singleton updates, heartbeat, mode toggles, feed
connectivity status, Playwright browser state, redeem events, and
cross-source price lookups (Chainlink, Tiingo, CLOB, Macro signals).

Delegates to the **exact same SQL** that ``engine/persistence/db_client.py``
uses today.  This is a thin structural split -- zero behaviour change.

Phase 2 will wire this into the composition root.  Until then, nothing
imports this module so there is zero runtime risk.

Audit: CA-01 (Clean Architecture migration -- split god-class DBClient).
"""

from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg
import structlog

log = structlog.get_logger(__name__)


class PgSystemRepository:
    """asyncpg-backed system state repository.

    Accepts an ``asyncpg.Pool`` -- the same pool the legacy ``DBClient``
    uses.  Methods copy SQL verbatim from ``db_client.py`` so behaviour
    parity is byte-for-byte.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # -- System State -------------------------------------------------------

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
        """Upsert the engine's current system state (single-row singleton record).

        Verbatim SQL from ``DBClient.update_system_state``.
        """
        if not self._pool:
            return

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

    async def write_heartbeat(self, row) -> None:
        """Persist a HeartbeatRow to system_state. Delegates to update_system_state."""
        await self.update_system_state(
            engine_status=row.engine_status,
            current_balance=row.current_balance,
            peak_balance=row.peak_balance,
            current_drawdown_pct=row.drawdown_pct,
            last_vpin=row.last_vpin,
            last_cascade_state=row.last_cascade_state,
            active_positions=row.active_positions,
            config=row.config_snapshot,
        )

    async def get_daily_record(self):
        """Return (wins_today, losses_today) from trade_bible."""
        if not self._pool:
            return 0, 0
        try:
            async with self._pool.acquire() as conn:
                wins = await conn.fetchval(
                    "SELECT COUNT(*) FROM trade_bible WHERE trade_outcome='WIN' AND is_live=true "
                    "AND resolved_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
                ) or 0
                losses = await conn.fetchval(
                    "SELECT COUNT(*) FROM trade_bible WHERE trade_outcome='LOSS' AND is_live=true "
                    "AND resolved_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
                ) or 0
                return int(wins), int(losses)
        except Exception:
            return 0, 0

    async def update_heartbeat(self) -> None:
        """Update last_heartbeat to NOW() without touching other fields.

        Verbatim SQL from ``DBClient.update_heartbeat``.
        """
        if not self._pool:
            return

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
            # Don't re-raise -- heartbeat failure is not fatal

    async def get_mode_toggles(self) -> dict | None:
        """Read paper_enabled / live_enabled from system_state.

        Verbatim SQL from ``DBClient.get_mode_toggles``.
        """
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
        """Update feed connection status boolean flags in system_state.

        Verbatim SQL from ``DBClient.update_feed_status``.
        Only updates columns that are explicitly passed (not None).
        """
        if not self._pool:
            return

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
            # Don't re-raise -- status update failure is not fatal

    # -- Playwright State ---------------------------------------------------

    async def ensure_playwright_tables(self) -> None:
        """Create playwright_state and redeem_events tables if they don't exist.

        Verbatim SQL from ``DBClient.ensure_playwright_tables``.
        """
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
            await conn.execute(
                "ALTER TABLE redeem_events ADD COLUMN IF NOT EXISTS redeem_type TEXT DEFAULT 'all'"
            )
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
        cooldown_until: Optional[str] = None,
        cooldown_reason: Optional[str] = None,
    ) -> None:
        """Upsert the playwright_state singleton row.

        Verbatim SQL from ``DBClient.update_playwright_state``.
        """
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
        """Check if a manual redeem was requested via Hub API.

        Verbatim SQL from ``DBClient.check_redeem_requested``.
        """
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
        """Record a redeem sweep event.

        Verbatim SQL from ``DBClient.write_redeem_event``.
        """
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
                    "SELECT COALESCE(SUM(redeemed_count + failed_count), 0) FROM redeem_events WHERE created_at >= date_trunc('day', NOW())"
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

    # -- Price Lookups -------------------------------------------------------

    async def get_latest_chainlink_price(self, asset: str = "BTC") -> float | None:
        """Get the most recent Chainlink price for an asset.

        Verbatim SQL from ``DBClient.get_latest_chainlink_price``.
        """
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
        """Get the most recent Tiingo price for an asset.

        Verbatim SQL from ``DBClient.get_latest_tiingo_price``.
        """
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
        """Get the most recent macro observer signal (< 5 min old).

        Verbatim SQL from ``DBClient.get_latest_macro_signal``.
        """
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

    async def get_latest_clob_prices(self, asset: str = "BTC") -> dict | None:
        """Get the most recent CLOB book prices for an asset.

        Verbatim SQL from ``DBClient.get_latest_clob_prices``.
        """
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
