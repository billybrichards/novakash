"""
Hyperliquid public price feed — polls /info {"type":"allMids"}.

This is the "paper mode Hyperliquid" price source. The PaperExchangeAdapter
takes an optional sync `price_getter` callable; wire this feed's get_price
method to that callable and the simulator fills every paper trade against
real Hyperliquid book midpoints.

Why polling instead of WebSocket:
  Hyperliquid offers a WS price stream, but for paper trading at ~2s cadence
  polling /info is simpler, stateless, and avoids WS reconnect complexity.
  The margin engine's tick_interval_s is also 2s, so one HL poll per tick
  is enough. Bumping to sub-second latency would require WS — that's a
  future change, not needed for the 15m-horizon strategy.

Failure modes:
  - Never succeeded (first poll fails): get_price() returns None. Paper
    adapter falls back to its internal _last_price (initialised at 80000).
    Loud WARN so operators notice.
  - Succeeded once, now stale: get_price() returns None after freshness_s.
    Same fallback. Loud WARN.
  - Recovered: next successful poll refreshes the cache.

No aggressive retries — the next scheduled poll handles recovery. The only
latency cost of a transient HL outage is one tick of stale-price paper
trading, which is acceptable for a 15-minute-hold strategy.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class HyperliquidPriceFeed:
    """
    Polls POST https://api.hyperliquid.xyz/info with {"type":"allMids"}
    and caches the latest mid price for a single asset (default BTC).

    Lifecycle:
        feed = HyperliquidPriceFeed(info_url="...", asset="BTC")
        await feed.connect()              # starts background poll task
        price = feed.get_price()          # sync, returns Optional[float]
        info = feed.info()                # dict for status_server
        await feed.disconnect()           # stops task, closes session

    Thread-safety note: get_price() is sync because PaperExchangeAdapter
    calls it from a sync context (inside its async methods, but without
    await). Reading a single float under GIL is atomic for CPython.
    """

    def __init__(
        self,
        info_url: str = "https://api.hyperliquid.xyz/info",
        asset: str = "BTC",
        poll_interval_s: float = 2.0,
        freshness_s: float = 15.0,
        request_timeout_s: float = 5.0,
    ) -> None:
        self._info_url = info_url
        self._asset = asset.upper()
        self._poll_interval_s = poll_interval_s
        self._freshness_s = freshness_s
        self._request_timeout_s = request_timeout_s

        self._latest_mid: Optional[float] = None
        self._latest_at: float = 0.0
        self._ever_succeeded: bool = False

        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Start the polling loop. Performs one synchronous initial fetch so
        get_price() returns a real number immediately instead of None on
        the first tick.
        """
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._request_timeout_s),
        )
        await self._poll_once()
        if not self._ever_succeeded:
            logger.warning(
                "HyperliquidPriceFeed: initial fetch failed — paper adapter "
                "will use _last_price fallback until a poll succeeds"
            )
        else:
            logger.info(
                "HyperliquidPriceFeed connected (initial mid %s=%.2f)",
                self._asset, self._latest_mid,
            )
        self._task = asyncio.create_task(
            self._poll_loop(), name="hyperliquid-price-poll",
        )

    async def disconnect(self) -> None:
        """Cancel the polling task and close the HTTP session."""
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("HyperliquidPriceFeed disconnected")

    # ── Public interface ──────────────────────────────────────────────────

    def get_price(self) -> Optional[float]:
        """
        Sync. Returns the cached mid price if it is fresh (< freshness_s old),
        otherwise None so the caller can apply its own fallback.
        """
        if self._latest_mid is None:
            return None
        age = time.time() - self._latest_at
        if age > self._freshness_s:
            return None
        return self._latest_mid

    @property
    def is_healthy(self) -> bool:
        """True when the cache is fresh and a real mid has been received."""
        if self._latest_mid is None:
            return False
        return (time.time() - self._latest_at) <= self._freshness_s

    def info(self) -> dict:
        """
        Observability payload for the status_server execution block.

        Used by the dashboard's price-feed dot and hover tooltip so
        operators can see at a glance whether HL is actually reachable.
        """
        age: Optional[float]
        if self._latest_at > 0:
            age = round(time.time() - self._latest_at, 2)
        else:
            age = None
        return {
            "source": "hyperliquid",
            "healthy": self.is_healthy,
            "last_price": self._latest_mid,
            "last_price_age_s": age,
            "asset": self._asset,
        }

    # ── Internals ─────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """
        Polls /info on a fixed cadence until cancelled.

        Uses asyncio.Event.wait with a timeout instead of asyncio.sleep so
        disconnect() can wake the loop instantly rather than waiting for
        the next tick.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval_s)
            except asyncio.TimeoutError:
                pass  # expected — this is the "next tick" branch
            if self._stop.is_set():
                break
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("HyperliquidPriceFeed poll loop error: %s", e)

    async def _poll_once(self) -> None:
        """
        Single HTTP POST to /info with {"type":"allMids"}.

        Parses the response as {asset: stringified_float, ...}, extracts
        our asset, and updates the cache. Logs WARN on failure but does
        NOT raise — the caller (connect / poll_loop) handles continuation.
        """
        if self._session is None:
            logger.warning("HyperliquidPriceFeed: poll_once called before connect()")
            return
        try:
            async with self._session.post(
                self._info_url, json={"type": "allMids"},
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "HyperliquidPriceFeed HTTP %d: %s",
                        resp.status, (await resp.text())[:200],
                    )
                    return
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("HyperliquidPriceFeed request failed: %s", e)
            return

        raw = payload.get(self._asset)
        if raw is None:
            logger.warning(
                "HyperliquidPriceFeed: asset %s not in allMids payload "
                "(got %d symbols)", self._asset, len(payload),
            )
            return

        try:
            mid = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "HyperliquidPriceFeed: unparseable mid %r for %s",
                raw, self._asset,
            )
            return

        self._latest_mid = mid
        self._latest_at = time.time()
        if not self._ever_succeeded:
            self._ever_succeeded = True
        logger.debug(
            "HyperliquidPriceFeed: %s=%.2f (age=0s)", self._asset, mid,
        )
