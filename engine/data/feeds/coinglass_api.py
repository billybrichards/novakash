"""
CoinGlass API Feed

Polls CoinGlass for:
  - Open Interest snapshots (with delta vs previous)
  - Liquidation volume (rolling window)

Uses REST polling rather than WebSocket (CoinGlass free tier limitation).
Interval: configurable, default 30 seconds.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Callable, Awaitable
import aiohttp
import structlog

from data.models import OpenInterestSnapshot, LiquidationVolume

log = structlog.get_logger(__name__)

COINGLASS_BASE = "https://open-api.coinglass.com/public/v2"
POLL_INTERVAL = 30  # seconds


class CoinGlassAPIFeed:
    """
    Polls CoinGlass REST API for OI and liquidation data.

    Computes the OI delta percentage between consecutive snapshots
    so downstream components don't need to track state.
    """

    def __init__(
        self,
        api_key: str,
        symbol: str = "BTC",
        poll_interval: int = POLL_INTERVAL,
        on_oi: Callable[[OpenInterestSnapshot], Awaitable[None]] | None = None,
        on_liq: Callable[[LiquidationVolume], Awaitable[None]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.symbol = symbol
        self.poll_interval = poll_interval
        self._on_oi = on_oi
        self._on_liq = on_liq
        self._running = False
        self._prev_oi: Decimal | None = None

    async def start(self) -> None:
        """Start polling loop."""
        self._running = True
        async with aiohttp.ClientSession(headers={"coinglassSecret": self.api_key}) as session:
            while self._running:
                try:
                    await self._poll(session)
                except Exception as exc:
                    log.error("coinglass.poll_error", error=str(exc))
                await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        """Stop polling loop."""
        self._running = False

    async def _poll(self, session: aiohttp.ClientSession) -> None:
        """Fetch OI and liquidation data from CoinGlass."""
        await self._fetch_oi(session)
        await self._fetch_liquidations(session)

    async def _fetch_oi(self, session: aiohttp.ClientSession) -> None:
        """Fetch open interest for the symbol."""
        url = f"{COINGLASS_BASE}/indicator/open_interest"
        params = {"symbol": self.symbol, "interval": "0"}

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            body = await resp.json()

        # Extract total OI from response
        data = body.get("data", {})
        total_oi_str = data.get("openInterestUsd", "0")
        oi = Decimal(str(total_oi_str))

        delta_pct = 0.0
        if self._prev_oi and self._prev_oi > 0:
            delta_pct = float((oi - self._prev_oi) / self._prev_oi)

        self._prev_oi = oi

        snapshot = OpenInterestSnapshot(
            symbol=self.symbol,
            open_interest_usd=oi,
            open_interest_delta_pct=delta_pct,
            timestamp=datetime.utcnow(),
        )

        if self._on_oi:
            await self._on_oi(snapshot)

    async def _fetch_liquidations(self, session: aiohttp.ClientSession) -> None:
        """Fetch liquidation volume in recent window."""
        url = f"{COINGLASS_BASE}/indicator/liquidation_history"
        params = {"symbol": self.symbol, "interval": "1h"}

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            body = await resp.json()

        data_list = body.get("data", [])
        if not data_list:
            return

        # Sum liquidations from the last hour
        total_liq = sum(Decimal(str(d.get("liquidationUsd", 0))) for d in data_list[-2:])

        liq = LiquidationVolume(
            symbol=self.symbol,
            liq_volume_usd=total_liq,
            window_seconds=3600,
            timestamp=datetime.utcnow(),
        )

        if self._on_liq:
            await self._on_liq(liq)
