"""
CLOB Book Feed — Real-time Polymarket order book prices

Queries the Polymarket CLOB directly for real bid/ask prices.
This is the GROUND TRUTH — what you actually pay when you trade.
Gamma API's bestAsk can be stale/smoothed. CLOB is live.

MUST run on Montreal only (Polymarket geo-blocked elsewhere).

Polls every 2 seconds (configurable via CLOB_POLL_INTERVAL env var).
Stores to ticks_clob table.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

POLL_INTERVAL = int(os.environ.get("CLOB_POLL_INTERVAL", "2"))  # seconds — need fresh prices for FOK/GTC


class CLOBFeed:
    """Polls Polymarket CLOB order book for real-time bid/ask prices."""

    def __init__(self, poly_client, db_pool, polymarket_feed=None):
        """
        Args:
            poly_client: PolymarketClient instance (has _clob_client for book queries)
            db_pool: asyncpg connection pool for Railway DB writes
            polymarket_feed: Polymarket5MinFeed to get current window token IDs
        """
        self._poly = poly_client
        self._pool = db_pool
        self._feed = polymarket_feed
        self._running = False
        self._connected = False

    async def start(self) -> None:
        """Begin polling loop."""
        self._running = True
        log.info("clob_feed.starting", interval=POLL_INTERVAL)

        while self._running:
            try:
                await self._poll()
                self._connected = True
            except Exception as exc:
                log.error("clob_feed.poll_error", error=str(exc)[:100])
                self._connected = False
            await asyncio.sleep(POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        """Fetch CLOB book for current window's tokens."""
        if not self._feed or not self._poly or self._poly.paper_mode:
            return

        # Get current window info from the feed
        window = self._feed.get_current_window("BTC")
        if not window or not window.up_token_id or not window.down_token_id:
            return

        try:
            if not self._poly._clob_client:
                return

            client = self._poly._clob_client

            # Fetch UP token book
            # CLOB asks are sorted DESCENDING — best (lowest) ask is LAST
            # CLOB bids are sorted DESCENDING — best (highest) bid is FIRST
            up_book = await asyncio.to_thread(client.get_order_book, window.up_token_id)
            up_best_bid = float(up_book.bids[0].price) if up_book.bids else None
            up_best_ask = float(sorted(up_book.asks, key=lambda x: float(x.price))[0].price) if up_book.asks else None

            # Fetch DOWN token book
            down_book = await asyncio.to_thread(client.get_order_book, window.down_token_id)
            down_best_bid = float(down_book.bids[0].price) if down_book.bids else None
            down_best_ask = float(sorted(down_book.asks, key=lambda x: float(x.price))[0].price) if down_book.asks else None

            up_spread = (up_best_ask - up_best_bid) if (up_best_ask and up_best_bid) else None
            down_spread = (down_best_ask - down_best_bid) if (down_best_ask and down_best_bid) else None

            # Mid price = (up_best_ask + (1 - down_best_ask)) / 2 if available
            mid = None
            if up_best_ask and down_best_ask:
                mid = round((up_best_ask + (1.0 - down_best_ask)) / 2, 4)

            log.info(
                "clob_feed.prices",
                asset="BTC",
                up_bid=f"${up_best_bid:.4f}" if up_best_bid else "—",
                up_ask=f"${up_best_ask:.4f}" if up_best_ask else "—",
                dn_bid=f"${down_best_bid:.4f}" if down_best_bid else "—",
                dn_ask=f"${down_best_ask:.4f}" if down_best_ask else "—",
                mid=f"${mid:.4f}" if mid else "—",
            )

            # Write to DB
            if self._pool:
                try:
                    async with self._pool.acquire() as conn:
                        # Update existing ticks_clob for backwards compatibility
                        await conn.execute(
                            """
                            INSERT INTO ticks_clob (
                                ts, asset, timeframe, window_ts,
                                up_token_id, down_token_id,
                                up_best_bid, up_best_ask,
                                down_best_bid, down_best_ask,
                                up_spread, down_spread
                            ) VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                            """,
                            "BTC", "5m", window.window_ts,
                            window.up_token_id, window.down_token_id,
                            up_best_bid, up_best_ask,
                            down_best_bid, down_best_ask,
                            up_spread, down_spread,
                        )
                        
                        # Write comprehensive snapshot to new table
                        await conn.execute(
                            """
                            INSERT INTO clob_book_snapshots (
                                asset, timeframe, window_ts,
                                up_token_id, down_token_id,
                                up_best_bid, up_best_ask,
                                down_best_bid, down_best_ask,
                                up_spread, down_spread
                            ) VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                            ON CONFLICT (window_ts, up_token_id, down_token_id, ts) DO NOTHING
                            """,
                            "BTC", "5m", window.window_ts,
                            window.up_token_id, window.down_token_id,
                            up_best_bid, up_best_ask,
                            down_best_bid, down_best_ask,
                            up_spread, down_spread,
                        )
                except Exception as exc:
                    log.error("clob_feed.write_error", error=str(exc)[:80])

        except Exception as exc:
            log.warning("clob_feed.book_error", error=str(exc)[:100])
