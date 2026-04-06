"""
Tiingo Multi-Asset Crypto Feed

Polls Tiingo's top-of-book API every 2 seconds for BTC, ETH, SOL, XRP.
Shows best bid/ask exchange — important for oracle matching against Chainlink.

API endpoint:
    https://api.tiingo.com/tiingo/crypto/top?tickers=btcusd,ethusd,solusd,xrpusd&token=KEY

Data written to: ticks_tiingo table in Railway PostgreSQL.

Tiingo key: TIINGO_API_KEY from .env
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Asset ticker map: internal asset name → Tiingo ticker
TICKERS = {
    "BTC": "btcusd",
    "ETH": "ethusd",
    "SOL": "solusd",
    "XRP": "xrpusd",
}

POLL_INTERVAL = 5  # seconds (Tiingo rate limits at ~2s)
API_BASE = "https://api.tiingo.com/tiingo/crypto/top"
SOURCE = "tiingo"


class TiingoFeed:
    """
    Polls Tiingo crypto top-of-book API every 2 seconds.

    Writes to ticks_tiingo table. Runs as an async background task.
    Logs which exchange has best bid/ask for oracle cross-referencing.

    Attributes:
        connected: True while polling is active and last poll succeeded.
        last_message_at: Timestamp of the most recent successful poll.
    """

    def __init__(self, api_key: str, pool) -> None:
        """
        Args:
            api_key: Tiingo API key (TIINGO_API_KEY from .env)
            pool:    asyncpg.Pool from DBClient._pool for ticks_tiingo writes
        """
        self._api_key = api_key
        self._pool = pool
        self._running = False
        self._connected = False
        self._last_message_at: Optional[datetime] = None
        self._session = None
        self._tickers_param = ",".join(TICKERS.values())

    # ─── Public Status ────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self) -> Optional[datetime]:
        return self._last_message_at

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start HTTP session and polling loop."""
        try:
            import aiohttp
        except ImportError:
            log.error("tiingo_feed.aiohttp_not_installed")
            return

        import aiohttp

        log.info(
            "tiingo_feed.starting",
            assets=list(TICKERS.keys()),
            interval=POLL_INTERVAL,
        )

        self._running = True
        async with aiohttp.ClientSession(
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Token {self._api_key}",
            }
        ) as session:
            self._session = session
            while self._running:
                try:
                    await self._poll(session)
                    self._connected = True
                    self._last_message_at = datetime.now(timezone.utc)
                except Exception as exc:
                    log.error("tiingo_feed.poll_error", error=str(exc))
                    self._connected = False
                await asyncio.sleep(POLL_INTERVAL)

        self._session = None

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        self._connected = False
        log.info("tiingo_feed.stopped")

    # ─── Internal ─────────────────────────────────────────────────────────────

    async def _poll(self, session) -> None:
        """Fetch top-of-book data for all tickers and write to DB."""
        import aiohttp

        url = f"{API_BASE}?tickers={self._tickers_param}&token={self._api_key}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status}")
            data = await resp.json()

        rows = []
        for item in data:
            ticker = item.get("ticker", "").lower()
            # Map ticker back to asset
            asset = next(
                (k for k, v in TICKERS.items() if v == ticker), None
            )
            if not asset:
                continue

            # Tiingo top-of-book structure: topOfBookData is a list, take [0]
            tob_list = item.get("topOfBookData", [])
            if not tob_list:
                continue
            tob = tob_list[0]

            last_price = _safe_float(tob.get("lastPrice") or tob.get("last"))
            bid_price = _safe_float(tob.get("bidPrice") or tob.get("bid"))
            ask_price = _safe_float(tob.get("askPrice") or tob.get("ask"))
            bid_exchange = _safe_str(tob.get("bidExchange") or tob.get("bidSizeExchange"))
            ask_exchange = _safe_str(tob.get("askExchange") or tob.get("askSizeExchange"))
            last_exchange = _safe_str(tob.get("lastExchange") or tob.get("exchange"))

            log.debug(
                "tiingo_feed.tick",
                asset=asset,
                last=last_price,
                bid=f"{bid_price} @ {bid_exchange}",
                ask=f"{ask_price} @ {ask_exchange}",
            )

            rows.append((
                asset,
                last_price,
                bid_price,
                ask_price,
                bid_exchange,
                ask_exchange,
                last_exchange,
            ))

        if rows:
            await self._write_rows(rows)

    async def _write_rows(self, rows: list[tuple]) -> None:
        """Batch INSERT rows into ticks_tiingo."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO ticks_tiingo (
                        ts, asset,
                        last_price, bid_price, ask_price,
                        bid_exchange, ask_exchange, last_exchange,
                        source
                    ) VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    [(*row, SOURCE) for row in rows],
                )
            log.debug("tiingo_feed.written", rows=len(rows))
        except Exception as exc:
            log.debug("tiingo_feed.write_error", error=str(exc))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _safe_str(val) -> Optional[str]:
    try:
        return str(val)[:20] if val is not None else None
    except Exception:
        return None
