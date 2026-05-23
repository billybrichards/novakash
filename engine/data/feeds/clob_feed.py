"""
CLOB Book Feed — Real-time Polymarket order book prices

Queries the Polymarket CLOB directly for real bid/ask prices.
This is the GROUND TRUTH — what you actually pay when you trade.
Gamma API's bestAsk can be stale/smoothed. CLOB is live.

MUST run on Montreal only (Polymarket geo-blocked elsewhere).

Polls every 2 seconds (configurable via CLOB_POLL_INTERVAL env var).
Stores to ticks_clob and clob_book_snapshots tables.

### Multi-asset (PR #583, follow-up to PR #582)

Each poll cycle iterates the assets configured via the
``FIVE_MIN_ASSETS`` env var (default ``BTC``) — same source of truth
the 5m strategy uses, so the CLOB poller stays aligned with what's
trading. Per-asset failures are isolated: a Polymarket 404 for one
asset (e.g. an ETH/XRP market that doesn't have a live updown contract
this minute) does not abort the others.

Polymarket DOES list multi-asset 5m updown markets (BTC, ETH, SOL,
XRP, …) with slug prefixes ``<asset>-updown-5m-{ts}``. The 5m feed
already discovers token IDs per asset, so by the time CLOBFeed polls,
``window.up_token_id`` / ``window.down_token_id`` are populated for
each enabled asset whenever a market exists. Assets without a current
market quietly skip (window=None) — that's the documented partial-
coverage failure mode.

The in-memory ``self.latest_clob`` dict preserves BTC-only semantics
for backwards compatibility (``data_surface.py:1204`` reads it
directly). A new ``self.latest_clob_by_asset`` keyed by asset stores
the per-asset snapshots for callers that want non-BTC fast-path
access.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

POLL_INTERVAL = int(
    os.environ.get("CLOB_POLL_INTERVAL", "2")
)  # seconds — need fresh prices for FOK/GTC


def _resolve_assets() -> list[str]:
    """Read FIVE_MIN_ASSETS at poll time so env changes pick up on next cycle."""
    raw = os.environ.get("FIVE_MIN_ASSETS", "BTC")
    assets = [a.strip().upper() for a in raw.split(",") if a.strip()]
    return assets or ["BTC"]


class CLOBFeed:
    """Polls Polymarket CLOB order book for real-time bid/ask prices."""

    def __init__(self, poly_client, db_pool, polymarket_feed=None):
        """
        Args:
            poly_client: PolymarketClient instance (exposes clob_client property for book queries)
            db_pool: asyncpg connection pool for Railway DB writes
            polymarket_feed: Polymarket5MinFeed to get current window token IDs
        """
        self._poly = poly_client
        self._pool = db_pool
        self._feed = polymarket_feed
        self._running = False
        self._connected = False
        # In-memory cache: updated on EVERY poll tick.
        # Read by DataSurfaceManager for zero-I/O CLOB price access.
        #
        # BACKWARDS COMPAT: `latest_clob` mirrors the BTC snapshot only.
        # `data_surface.py:1204` reads it as a flat dict and was written
        # before the feed was multi-asset; preserve that contract.
        #
        # Multi-asset callers should read `latest_clob_by_asset[asset]`
        # which carries the same dict shape per asset.
        self.latest_clob: dict = {}
        self.latest_clob_by_asset: dict[str, dict] = {}

    async def start(self) -> None:
        """Begin polling loop."""
        self._running = True
        log.info(
            "clob_feed.starting",
            interval=POLL_INTERVAL,
            paper_mode="enabled",
            assets=_resolve_assets(),
        )

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
        """Fetch CLOB book for every enabled asset's current window.

        Each asset is polled independently — a failure on one (e.g. no
        live ETH market this minute, Polymarket 5xx, geo-block) is
        logged and skipped without aborting the others.
        """
        if not self._feed or not self._poly:
            return

        assets = _resolve_assets()
        for asset in assets:
            try:
                await self._poll_asset(asset)
            except Exception as exc:
                # Per-asset isolation: never let one bad asset abort the loop.
                log.warning(
                    "clob_feed.asset_error",
                    asset=asset,
                    error=str(exc)[:100],
                )

    async def _poll_asset(self, asset: str) -> None:
        """Poll a single asset's CLOB book and write to DB."""
        # Get current window info from the feed
        window = self._feed.get_current_window(asset)
        if not window or not window.up_token_id or not window.down_token_id:
            # No active market for this asset right now — quietly skip.
            return

        # Derive timeframe from window duration
        timeframe = "15m" if window.duration_secs >= 900 else "5m"

        try:
            # Use the new get_clob_order_book method which works in both paper and live mode
            up_book = await self._poly.get_clob_order_book(window.up_token_id)
            down_book = await self._poly.get_clob_order_book(window.down_token_id)

            # Extract best bid/ask from the book structure
            up_best_bid = up_book.get("best_bid") if up_book else None
            up_best_ask = up_book.get("best_ask") if up_book else None
            down_best_bid = down_book.get("best_bid") if down_book else None
            down_best_ask = down_book.get("best_ask") if down_book else None

            up_spread = (
                (up_best_ask - up_best_bid) if (up_best_ask and up_best_bid) else None
            )
            down_spread = (
                (down_best_ask - down_best_bid)
                if (down_best_ask and down_best_bid)
                else None
            )

            # Mid price = (up_best_ask + (1 - down_best_ask)) / 2 if available
            mid = None
            if up_best_ask and down_best_ask:
                mid = round((up_best_ask + (1.0 - down_best_ask)) / 2, 4)

            # Update in-memory caches on every poll tick.
            # last_updated lets callers detect a stale cache after restart
            # (e.g. reject if time.time() - last_updated > 30s).
            snapshot = {
                "clob_up_bid": up_best_bid,
                "clob_up_ask": up_best_ask,
                "clob_down_bid": down_best_bid,
                "clob_down_ask": down_best_ask,
                "clob_implied_up": mid,
                "last_updated": time.time(),
            }
            self.latest_clob_by_asset[asset] = snapshot
            # BTC mirrors into `latest_clob` to preserve the pre-multi-asset
            # contract (data_surface.py:1204 reads a flat dict).
            if asset == "BTC":
                self.latest_clob = snapshot

            log.info(
                "clob_feed.prices",
                asset=asset,
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
                            asset,
                            timeframe,
                            window.window_ts,
                            window.up_token_id,
                            window.down_token_id,
                            up_best_bid,
                            up_best_ask,
                            down_best_bid,
                            down_best_ask,
                            up_spread,
                            down_spread,
                        )

                        # Write comprehensive snapshot to new table.
                        # PE-01 fix: the column list was missing `ts` so the 11
                        # column names did not line up with the 11 VALUES slots
                        # the Python call was passing — NOW() was consuming one
                        # column slot but no positional parameter. asyncpg
                        # reported "server expects 10 arguments for this query,
                        # 11 were passed". Adding `ts` first + a fresh $11
                        # keeps NOW() inline as the ts value and makes the
                        # column / VALUES / Python-arg counts all reconcile.
                        await conn.execute(
                            """
                            INSERT INTO clob_book_snapshots (
                                ts, asset, timeframe, window_ts,
                                up_token_id, down_token_id,
                                up_best_bid, up_best_ask,
                                down_best_bid, down_best_ask,
                                up_spread, down_spread
                            ) VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                            ON CONFLICT (window_ts, up_token_id, down_token_id, ts) DO NOTHING
                            """,
                            asset,
                            timeframe,
                            window.window_ts,
                            window.up_token_id,
                            window.down_token_id,
                            up_best_bid,
                            up_best_ask,
                            down_best_bid,
                            down_best_ask,
                            up_spread,
                            down_spread,
                        )
                except Exception as exc:
                    log.error(
                        "clob_feed.write_error", asset=asset, error=str(exc)[:80]
                    )

        except Exception as exc:
            log.warning("clob_feed.book_error", asset=asset, error=str(exc)[:100])
