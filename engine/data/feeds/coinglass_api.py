"""
CoinGlass API Feed

Polls CoinGlass for:
  - Open Interest snapshots (with delta vs previous)
  - Liquidation volume (rolling 5-minute window from Binance forceOrder events,
    and hourly from CoinGlass REST as a fallback/longer-term signal)

Uses REST polling rather than WebSocket (CoinGlass free tier limitation).
Interval: configurable, default 30 seconds.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Awaitable, Optional
import aiohttp
import structlog

from data.models import OpenInterestSnapshot, LiquidationVolume

log = structlog.get_logger(__name__)

COINGLASS_BASE = "https://open-api-v4.coinglass.com/api"
POLL_INTERVAL = 30  # seconds
LIQ_WINDOW_SECONDS = 300  # 5 minutes


class CoinGlassAPIFeed:
    """
    Polls CoinGlass REST API for OI and liquidation data.

    Computes the OI delta percentage between consecutive snapshots.
    Tracks a 5-minute rolling liquidation volume window.

    Attributes:
        connected: True while the polling loop is running without error.
        last_message_at: Timestamp of the most recent successful poll.
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
        self._connected = False
        self._last_message_at: Optional[datetime] = None
        self._prev_oi: Optional[Decimal] = None
        # Rolling 5-minute liq window: deque of (timestamp, value_usd)
        self._liq_window: deque[tuple[datetime, Decimal]] = deque()

    # ─── Public Status Properties ──────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """True if the polling loop is active and last poll succeeded."""
        return self._connected

    @property
    def last_message_at(self) -> Optional[datetime]:
        """Timestamp of the last successful poll."""
        return self._last_message_at

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start polling loop."""
        self._running = True
        headers = {
            "CG-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            while self._running:
                try:
                    await self._poll(session)
                    self._connected = True
                    self._last_message_at = datetime.utcnow()
                except aiohttp.ClientResponseError as exc:
                    if exc.status == 429:
                        log.warning("coinglass.rate_limited", retry_in=60)
                        self._connected = False
                        await asyncio.sleep(60)
                        continue
                    log.error("coinglass.http_error", status=exc.status, error=str(exc))
                    self._connected = False
                except Exception as exc:
                    log.error("coinglass.poll_error", error=str(exc))
                    self._connected = False
                await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        """Stop polling loop."""
        self._running = False
        self._connected = False
        log.info("coinglass.stopped")

    # ─── Internal ─────────────────────────────────────────────────────────────

    async def _poll(self, session: aiohttp.ClientSession) -> None:
        """Fetch OI and liquidation data from CoinGlass."""
        await self._fetch_oi(session)
        await self._fetch_liquidations(session)

    async def _fetch_oi(self, session: aiohttp.ClientSession) -> None:
        """Fetch open interest for the symbol via CoinGlass v4 API."""
        url = f"{COINGLASS_BASE}/futures/open-interest/history"
        # Standard plan: 1m intervals now available
        params = {"symbol": f"{self.symbol}USDT", "interval": "1m", "limit": "2", "exchange": "Binance"}

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            body = await resp.json()

        if body.get("code") != "0":
            log.warning("coinglass.oi_api_error", msg=body.get("msg", "unknown"))
            return

        data = body.get("data", [])
        if not data:
            log.debug("coinglass.oi_empty_response")
            return

        # v4 API returns OHLC candles — use the latest close value
        latest = data[-1] if isinstance(data, list) else data
        total_oi = Decimal(str(latest.get("close", 0)))

        delta_pct = 0.0
        if self._prev_oi and self._prev_oi > 0:
            delta_pct = float((total_oi - self._prev_oi) / self._prev_oi)

        self._prev_oi = total_oi

        snapshot = OpenInterestSnapshot(
            symbol=self.symbol,
            open_interest_usd=total_oi,
            open_interest_delta_pct=delta_pct,
            timestamp=datetime.utcnow(),
        )

        log.debug("coinglass.oi_update", oi_usd=str(total_oi), delta_pct=f"{delta_pct:.4f}")

        if self._on_oi:
            await self._on_oi(snapshot)

    async def _fetch_liquidations(self, session: aiohttp.ClientSession) -> None:
        """Fetch liquidation volume and update the 5-minute rolling window."""
        url = f"{COINGLASS_BASE}/futures/liquidation/history"
        # Standard plan: 1m intervals now available
        params = {"symbol": f"{self.symbol}USDT", "interval": "1m", "limit": "2", "exchange": "Binance"}

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            body = await resp.json()

        if body.get("code") != "0":
            log.warning("coinglass.liq_api_error", msg=body.get("msg", "unknown"))
            return

        data_list = body.get("data", [])
        if not data_list:
            return

        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=LIQ_WINDOW_SECONDS)

        # v4 API returns long_liquidation_usd + short_liquidation_usd
        if isinstance(data_list, list) and len(data_list) > 0:
            latest = data_list[-1]
            long_liq = Decimal(str(latest.get("long_liquidation_usd", 0)))
            short_liq = Decimal(str(latest.get("short_liquidation_usd", 0)))
            liq_val = long_liq + short_liq
            self._liq_window.append((now, liq_val))

        # Prune entries older than the window
        while self._liq_window and self._liq_window[0][0] < cutoff:
            self._liq_window.popleft()

        # Sum the rolling window
        rolling_total = sum(v for _, v in self._liq_window)

        liq = LiquidationVolume(
            symbol=self.symbol,
            liq_volume_usd=rolling_total,
            window_seconds=LIQ_WINDOW_SECONDS,
            timestamp=now,
        )

        log.debug("coinglass.liq_update", liq_5m_usd=str(rolling_total))

        if self._on_liq:
            await self._on_liq(liq)
