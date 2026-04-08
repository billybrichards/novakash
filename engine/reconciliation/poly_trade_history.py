"""
Polymarket Trade History Reconciler — Source of Truth

Fetches the ACTUAL trade history from Polymarket's CLOB API (your real
wallet fills) and cross-references with the engine's trades table.

This is the DEFINITIVE source of truth for:
- What trades were actually placed and filled
- At what price and size
- What the oracle outcome was (UP/DOWN)
- Whether we won or lost

Runs every 5 minutes on Montreal. Results written to `poly_trade_history`
table and used to validate/correct trade_bible entries.
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


class PolyTradeHistoryReconciler:
    """Fetches and stores Polymarket CLOB trade history."""

    def __init__(self, poly_client, db_pool, alerter, shutdown_event: asyncio.Event):
        self._poly = poly_client
        self._pool = db_pool
        self._alerter = alerter
        self._shutdown = shutdown_event
        self._log = log.bind(component="poly_history")
        self._table_ensured = False
        self._last_run: float = 0.0
        self._interval = 300  # 5 minutes

    async def _ensure_table(self):
        if self._table_ensured or not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS poly_trade_history (
                        id BIGSERIAL PRIMARY KEY,
                        fill_id TEXT UNIQUE,
                        asset_id TEXT NOT NULL,
                        outcome TEXT,
                        side TEXT,
                        price DOUBLE PRECISION,
                        size DOUBLE PRECISION,
                        cost DOUBLE PRECISION,
                        match_time BIGINT,
                        match_time_utc TIMESTAMPTZ,
                        status TEXT,
                        matched_trade_id INTEGER,
                        fetched_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_poly_hist_asset_time
                    ON poly_trade_history (asset_id, match_time DESC)
                """)
            self._table_ensured = True
            self._log.info("poly_history.table_ensured")
        except Exception as exc:
            self._log.warning("poly_history.table_error", error=str(exc)[:100])

    async def run(self):
        """Main loop: fetch and store trade history every 5 min."""
        self._log.info("poly_history.started")
        await self._ensure_table()

        while not self._shutdown.is_set():
            now = time.time()
            if now - self._last_run >= self._interval:
                self._last_run = now
                try:
                    await self._fetch_and_store()
                except Exception as exc:
                    self._log.warning("poly_history.error", error=str(exc)[:200])

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=30)
                break
            except asyncio.TimeoutError:
                pass

        self._log.info("poly_history.stopped")

    async def _fetch_and_store(self):
        """Fetch CLOB trade history and store/update."""
        if not self._pool:
            return

        try:
            fills = await self._poly.get_trade_history()
        except Exception as exc:
            self._log.warning("poly_history.fetch_error", error=str(exc)[:100])
            return

        if not fills:
            return

        new_count = 0
        matched_count = 0

        async with self._pool.acquire() as conn:
            for fill in fills:
                fill_id = fill.get("id", "")
                if not fill_id:
                    continue

                asset_id = fill.get("asset_id", "")
                outcome = fill.get("outcome", "")
                side = fill.get("side", "")
                price = float(fill.get("price", 0))
                size = float(fill.get("size", 0))
                cost = round(price * size, 4)
                match_time = int(fill.get("match_time", 0))
                status = fill.get("status", "")

                match_time_utc = datetime.fromtimestamp(match_time, tz=timezone.utc) if match_time else None

                # Try to match to engine trade by asset_id prefix
                matched_trade_id = None
                try:
                    match = await conn.fetchrow(
                        """SELECT id FROM trades
                           WHERE is_live = true
                             AND (metadata->>'token_id' LIKE $1 || '%'
                                  OR $2 LIKE metadata->>'token_id' || '%')
                           ORDER BY created_at DESC LIMIT 1""",
                        asset_id[:60],
                        asset_id,
                    )
                    if match:
                        matched_trade_id = match["id"]
                        matched_count += 1
                except Exception:
                    pass

                # Upsert
                try:
                    result = await conn.execute(
                        """INSERT INTO poly_trade_history
                           (fill_id, asset_id, outcome, side, price, size, cost,
                            match_time, match_time_utc, status, matched_trade_id)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                           ON CONFLICT (fill_id) DO UPDATE SET
                             outcome = EXCLUDED.outcome,
                             status = EXCLUDED.status,
                             matched_trade_id = COALESCE(EXCLUDED.matched_trade_id, poly_trade_history.matched_trade_id)""",
                        fill_id, asset_id, outcome, side, price, size, cost,
                        match_time, match_time_utc, status, matched_trade_id,
                    )
                    if "INSERT" in result:
                        new_count += 1
                except Exception as exc:
                    self._log.debug("poly_history.upsert_error", error=str(exc)[:80])

        if new_count > 0:
            self._log.info("poly_history.stored", new=new_count, matched=matched_count, total=len(fills))
