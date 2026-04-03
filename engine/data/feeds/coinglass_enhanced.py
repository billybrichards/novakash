"""
CoinGlass Enhanced Feed — 1-Minute Granularity

Requires CoinGlass Standard plan for ≤1min data intervals.
Polls every 10 seconds for:
  - OI delta (1-min candles)
  - Liquidation volume (1-min, long + short)
  - Long/Short ratio (global account ratio)
  - Top Traders L/S Position Ratio (1-min, smart money)
  - Taker Buy/Sell Volume (1-min, aggression signal)
  - Funding rate (8h history)

Exposes a snapshot object that the strategy reads on each evaluation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import structlog

log = structlog.get_logger(__name__)

COINGLASS_BASE = "https://open-api-v4.coinglass.com/api"
POLL_INTERVAL = 10  # seconds


@dataclass
class CoinGlassSnapshot:
    """Point-in-time snapshot of CoinGlass data for signal computation."""
    # OI
    oi_usd: float = 0.0
    oi_delta_pct_1m: float = 0.0       # 1-min OI change %

    # Liquidations (1-min)
    liq_long_usd_1m: float = 0.0       # Long liquidations in last minute
    liq_short_usd_1m: float = 0.0      # Short liquidations in last minute
    liq_total_usd_1m: float = 0.0      # Total liquidations in last minute

    # Long/Short ratio (global crowd)
    long_short_ratio: float = 1.0       # >1 = more longs, <1 = more shorts
    long_pct: float = 50.0             # % of accounts that are long
    short_pct: float = 50.0            # % of accounts that are short

    # Top Traders L/S Position Ratio (smart money)
    top_position_long_pct: float = 50.0    # Top traders long %
    top_position_short_pct: float = 50.0   # Top traders short %
    top_position_ratio: float = 1.0        # Top traders L/S ratio

    # Taker Buy/Sell Volume (1-min, aggression)
    taker_buy_volume_1m: float = 0.0    # Taker buy volume USD
    taker_sell_volume_1m: float = 0.0   # Taker sell volume USD

    # Funding rate
    funding_rate: float = 0.0           # Latest 8h funding rate (positive = longs pay)
    funding_rate_annual: float = 0.0    # Annualised

    # Meta
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    connected: bool = False
    last_error: Optional[str] = None


class CoinGlassEnhancedFeed:
    """
    Polls CoinGlass v4 API at 10s intervals for real-time derivatives data.

    The latest snapshot is always available via `self.snapshot`.
    """

    def __init__(
        self,
        api_key: str,
        symbol: str = "BTC",
        poll_interval: int = POLL_INTERVAL,
    ) -> None:
        self.api_key = api_key
        self.symbol = symbol
        self.poll_interval = poll_interval
        self.snapshot = CoinGlassSnapshot()
        self._running = False
        self._prev_oi: Optional[float] = None
        self._log = log.bind(component="coinglass_enhanced")

    @property
    def connected(self) -> bool:
        return self.snapshot.connected

    async def start(self) -> None:
        """Start the polling loop."""
        self._running = True
        headers = {
            "CG-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }

        self._log.info("coinglass_enhanced.starting", symbol=self.symbol)

        async with aiohttp.ClientSession(headers=headers) as session:
            while self._running:
                try:
                    await self._poll_all(session)
                    self.snapshot.connected = True
                    self.snapshot.last_error = None
                    self.snapshot.timestamp = datetime.now(timezone.utc)
                except aiohttp.ClientResponseError as exc:
                    if exc.status == 429:
                        self._log.warning("coinglass_enhanced.rate_limited", retry_in=30)
                        self.snapshot.connected = False
                        await asyncio.sleep(30)
                        continue
                    self._log.error("coinglass_enhanced.http_error", status=exc.status)
                    self.snapshot.connected = False
                    self.snapshot.last_error = f"HTTP {exc.status}"
                except Exception as exc:
                    self._log.error("coinglass_enhanced.poll_error", error=str(exc))
                    self.snapshot.connected = False
                    self.snapshot.last_error = str(exc)

                await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        self.snapshot.connected = False
        self._log.info("coinglass_enhanced.stopped")

    # ── Internal Polling ──────────────────────────────────────────────────────

    async def _poll_all(self, session: aiohttp.ClientSession) -> None:
        """Fetch all data points concurrently."""
        await asyncio.gather(
            self._fetch_oi(session),
            self._fetch_liquidations(session),
            self._fetch_long_short(session),
            self._fetch_top_position_ratio(session),
            self._fetch_taker_volume(session),
            self._fetch_funding(session),
            return_exceptions=True,
        )

    async def _fetch_oi(self, session: aiohttp.ClientSession) -> None:
        """Fetch 1-minute OI data."""
        url = f"{COINGLASS_BASE}/futures/open-interest/history"
        params = {
            "symbol": f"{self.symbol}USDT",
            "interval": "1m",
            "limit": "2",
            "exchange": "Binance",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            body = await resp.json()

        if body.get("code") != "0":
            return

        data = body.get("data", [])
        if not data or len(data) < 1:
            return

        latest = data[-1] if isinstance(data, list) else data
        oi = float(latest.get("close", 0))
        self.snapshot.oi_usd = oi

        if self._prev_oi and self._prev_oi > 0:
            self.snapshot.oi_delta_pct_1m = (oi - self._prev_oi) / self._prev_oi
        self._prev_oi = oi

    async def _fetch_liquidations(self, session: aiohttp.ClientSession) -> None:
        """Fetch 1-minute liquidation data."""
        url = f"{COINGLASS_BASE}/futures/liquidation/history"
        params = {
            "symbol": f"{self.symbol}USDT",
            "interval": "1m",
            "limit": "1",
            "exchange": "Binance",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            body = await resp.json()

        if body.get("code") != "0":
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        long_liq = float(latest.get("long_liquidation_usd", 0))
        short_liq = float(latest.get("short_liquidation_usd", 0))

        self.snapshot.liq_long_usd_1m = long_liq
        self.snapshot.liq_short_usd_1m = short_liq
        self.snapshot.liq_total_usd_1m = long_liq + short_liq

    async def _fetch_long_short(self, session: aiohttp.ClientSession) -> None:
        """Fetch global long/short account ratio."""
        url = f"{COINGLASS_BASE}/futures/global-long-short-account-ratio/history"
        params = {
            "symbol": f"{self.symbol}USDT",
            "interval": "1m",
            "limit": "1",
            "exchange": "Binance",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            body = await resp.json()

        if body.get("code") != "0":
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        ratio = float(latest.get("longShortRatio", 1.0))
        long_pct = float(latest.get("longRate", 50.0))
        short_pct = float(latest.get("shortRate", 50.0))

        self.snapshot.long_short_ratio = ratio
        self.snapshot.long_pct = long_pct
        self.snapshot.short_pct = short_pct

    async def _fetch_top_position_ratio(self, session: aiohttp.ClientSession) -> None:
        """Fetch top traders long/short position ratio (smart money positioning)."""
        url = f"{COINGLASS_BASE}/futures/top-long-short-position-ratio/history"
        params = {
            "symbol": f"{self.symbol}USDT",
            "interval": "1m",
            "limit": "1",
            "exchange": "Binance",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            body = await resp.json()

        if body.get("code") != "0":
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        long_pct = float(latest.get("longAccount", latest.get("top_position_long_percent", 50.0)))
        short_pct = float(latest.get("shortAccount", latest.get("top_position_short_percent", 50.0)))
        ratio = float(latest.get("longShortRatio", latest.get("top_position_long_short_ratio", 1.0)))

        self.snapshot.top_position_long_pct = long_pct
        self.snapshot.top_position_short_pct = short_pct
        self.snapshot.top_position_ratio = ratio

        self._log.debug(
            "coinglass_enhanced.top_position_ratio",
            long_pct=f"{long_pct:.1f}%",
            short_pct=f"{short_pct:.1f}%",
            ratio=f"{ratio:.3f}",
        )

    async def _fetch_taker_volume(self, session: aiohttp.ClientSession) -> None:
        """Fetch taker buy/sell volume (aggression signal)."""
        url = f"{COINGLASS_BASE}/futures/taker-buy-sell-volume/history"
        params = {
            "symbol": f"{self.symbol}USDT",
            "interval": "1m",
            "limit": "1",
            "exchange": "Binance",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            body = await resp.json()

        if body.get("code") != "0":
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        buy_vol = float(latest.get("taker_buy_volume_usd", latest.get("buyVolume", 0)))
        sell_vol = float(latest.get("taker_sell_volume_usd", latest.get("sellVolume", 0)))

        self.snapshot.taker_buy_volume_1m = buy_vol
        self.snapshot.taker_sell_volume_1m = sell_vol

        self._log.debug(
            "coinglass_enhanced.taker_volume",
            buy_vol_usd=f"${buy_vol:,.0f}",
            sell_vol_usd=f"${sell_vol:,.0f}",
            ratio=f"{buy_vol / sell_vol:.2f}" if sell_vol > 0 else "inf",
        )

    async def _fetch_funding(self, session: aiohttp.ClientSession) -> None:
        """Fetch latest funding rate from history (8h candles)."""
        url = f"{COINGLASS_BASE}/futures/funding-rate/history"
        params = {
            "symbol": f"{self.symbol}USDT",
            "exchange": "Binance",
            "interval": "8h",
            "limit": "1",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            body = await resp.json()

        if body.get("code") != "0":
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        rate = float(latest.get("fundingRate", latest.get("rate", 0)))
        self.snapshot.funding_rate = rate
        # Annualise: rate × 3 (8h periods/day) × 365
        self.snapshot.funding_rate_annual = rate * 3 * 365

        self._log.debug(
            "coinglass_enhanced.funding_rate",
            rate=f"{rate:.6f}",
            annual_pct=f"{rate * 3 * 365 * 100:.2f}%",
        )
