"""
CoinGlass Enhanced Feed — 5-Minute Granularity

Requires CoinGlass Standard plan for ≤1min data intervals.
Polls every 10 seconds for:
  - OI delta (5-min candles from Binance BTCUSDT)
  - Aggregated OI (all exchanges via exchange-list)
  - Liquidation volume (5-min, long + short, aggregated across exchanges)
  - Long/Short ratio (global account ratio from Binance)
  - Top Traders L/S Position Ratio (5-min, smart money from Binance)
  - Taker Buy/Sell Volume (5-min, aggregated across exchanges)
  - Funding rate (8h OHLC from Binance)

Exposes a snapshot object that the strategy reads on each evaluation.

v4 API Reference (verified 2026-04-03):
  - code is returned as STRING "0" for success
  - Per-exchange endpoints use symbol=BTCUSDT
  - Aggregated endpoints use symbol=BTC + exchange_list param
  - L/S ratio fields: global_account_long_percent, global_account_short_percent
  - Taker fields: aggregated_buy_volume_usd, aggregated_sell_volume_usd
  - Funding returns OHLC — use close for latest rate
  - OI exchange-list returns per-exchange data — sum for aggregate
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
    oi_delta_pct_1m: float = 0.0       # OI change % (from last poll)

    # Liquidations (aggregated across exchanges)
    liq_long_usd_1m: float = 0.0       # Long liquidations in last candle
    liq_short_usd_1m: float = 0.0      # Short liquidations in last candle
    liq_total_usd_1m: float = 0.0      # Total liquidations in last candle

    # Long/Short ratio (global crowd — Binance)
    long_short_ratio: float = 1.0       # >1 = more longs, <1 = more shorts
    long_pct: float = 50.0             # % of accounts that are long
    short_pct: float = 50.0            # % of accounts that are short

    # Top Traders L/S Position Ratio (smart money — Binance)
    top_position_long_pct: float = 50.0    # Top traders long %
    top_position_short_pct: float = 50.0   # Top traders short %
    top_position_ratio: float = 1.0        # Top traders L/S ratio

    # Taker Buy/Sell Volume (aggregated across exchanges)
    taker_buy_volume_1m: float = 0.0    # Taker buy volume USD
    taker_sell_volume_1m: float = 0.0   # Taker sell volume USD

    # Funding rate
    funding_rate: float = 0.0           # Latest 8h funding rate (positive = longs pay)
    funding_rate_annual: float = 0.0    # Annualised

    # Meta
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    connected: bool = False
    last_error: Optional[str] = None
    poll_count: int = 0                 # Total successful polls
    error_count: int = 0               # Total errors since last success


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
            "accept": "application/json",
        }

        self._log.info("coinglass_enhanced.starting", symbol=self.symbol)

        async with aiohttp.ClientSession(headers=headers) as session:
            while self._running:
                try:
                    await self._poll_all(session)
                    self.snapshot.connected = True
                    self.snapshot.last_error = None
                    self.snapshot.error_count = 0
                    self.snapshot.poll_count += 1
                    self.snapshot.timestamp = datetime.now(timezone.utc)
                except aiohttp.ClientResponseError as exc:
                    if exc.status == 429:
                        self._log.warning("coinglass_enhanced.rate_limited", retry_in=30)
                        self.snapshot.connected = False
                        self.snapshot.error_count += 1
                        await asyncio.sleep(30)
                        continue
                    self._log.error("coinglass_enhanced.http_error", status=exc.status)
                    self.snapshot.connected = False
                    self.snapshot.last_error = f"HTTP {exc.status}"
                    self.snapshot.error_count += 1
                except Exception as exc:
                    self._log.error("coinglass_enhanced.poll_error", error=str(exc))
                    self.snapshot.connected = False
                    self.snapshot.last_error = str(exc)[:100]
                    self.snapshot.error_count += 1

                await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        self.snapshot.connected = False
        self._log.info("coinglass_enhanced.stopped")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _is_success(body: dict) -> bool:
        """Check if API response indicates success. Code is STRING '0'."""
        code = body.get("code")
        return code == "0" or code == 0

    # ── Internal Polling ──────────────────────────────────────────────────────

    async def _poll_all(self, session: aiohttp.ClientSession) -> None:
        """Fetch all data points concurrently."""
        results = await asyncio.gather(
            self._fetch_oi(session),
            self._fetch_oi_aggregate(session),
            self._fetch_liquidations(session),
            self._fetch_long_short(session),
            self._fetch_top_position_ratio(session),
            self._fetch_taker_volume(session),
            self._fetch_funding(session),
            return_exceptions=True,
        )
        # Log any individual fetch errors at debug level
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                self._log.debug("coinglass_enhanced.fetch_error", index=i, error=str(r)[:80])

    async def _fetch_oi(self, session: aiohttp.ClientSession) -> None:
        """Fetch OI OHLC from Binance BTCUSDT (for delta calculation)."""
        url = f"{COINGLASS_BASE}/futures/open-interest/history"
        params = {
            "symbol": f"{self.symbol}USDT",
            "interval": "5m",
            "limit": "2",
            "exchange": "Binance",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            body = await resp.json()

        if not self._is_success(body):
            self._log.debug("coinglass_enhanced.oi_failed", code=body.get("code"), msg=body.get("msg", "")[:60])
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        # close can be string or float
        oi = float(latest.get("close", 0))
        self.snapshot.oi_usd = oi

        if self._prev_oi and self._prev_oi > 0:
            self.snapshot.oi_delta_pct_1m = (oi - self._prev_oi) / self._prev_oi
        self._prev_oi = oi

    async def _fetch_oi_aggregate(self, session: aiohttp.ClientSession) -> None:
        """Fetch aggregated OI across all exchanges (for total OI display)."""
        url = f"{COINGLASS_BASE}/futures/open-interest/exchange-list"
        params = {"symbol": self.symbol}

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            body = await resp.json()

        if not self._is_success(body):
            return

        data = body.get("data", [])
        if not data:
            return

        # Find the "All" exchange entry for aggregate, or sum manually
        for entry in data:
            if entry.get("exchange") == "All":
                total_oi = float(entry.get("open_interest_usd", 0))
                if total_oi > 0:
                    self.snapshot.oi_usd = total_oi
                break

    async def _fetch_liquidations(self, session: aiohttp.ClientSession) -> None:
        """Fetch aggregated liquidation data across exchanges."""
        url = f"{COINGLASS_BASE}/futures/liquidation/aggregated-history"
        params = {
            "symbol": self.symbol,
            "interval": "5m",
            "limit": "1",
            "exchange_list": "Binance,OKX,Bybit,Bitget,dYdX",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            body = await resp.json()

        if not self._is_success(body):
            self._log.debug("coinglass_enhanced.liq_failed", code=body.get("code"), msg=body.get("msg", "")[:60])
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        long_liq = float(latest.get("aggregated_long_liquidation_usd", 0))
        short_liq = float(latest.get("aggregated_short_liquidation_usd", 0))

        self.snapshot.liq_long_usd_1m = long_liq
        self.snapshot.liq_short_usd_1m = short_liq
        self.snapshot.liq_total_usd_1m = long_liq + short_liq

    async def _fetch_long_short(self, session: aiohttp.ClientSession) -> None:
        """Fetch global long/short account ratio from Binance."""
        url = f"{COINGLASS_BASE}/futures/global-long-short-account-ratio/history"
        params = {
            "symbol": f"{self.symbol}USDT",
            "interval": "5m",
            "limit": "1",
            "exchange": "Binance",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            body = await resp.json()

        if not self._is_success(body):
            self._log.debug("coinglass_enhanced.ls_failed", code=body.get("code"), msg=body.get("msg", "")[:60])
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        # v4 API field names (verified)
        ratio = float(latest.get("global_account_long_short_ratio",
                      latest.get("longShortRatio", 1.0)))
        long_pct = float(latest.get("global_account_long_percent",
                        latest.get("longRate", 50.0)))
        short_pct = float(latest.get("global_account_short_percent",
                         latest.get("shortRate", 50.0)))

        self.snapshot.long_short_ratio = ratio
        self.snapshot.long_pct = long_pct
        self.snapshot.short_pct = short_pct

        self._log.debug(
            "coinglass_enhanced.long_short_ratio",
            long_pct=f"{long_pct:.1f}%",
            short_pct=f"{short_pct:.1f}%",
            ratio=f"{ratio:.3f}",
        )

    async def _fetch_top_position_ratio(self, session: aiohttp.ClientSession) -> None:
        """Fetch top traders long/short position ratio (smart money positioning)."""
        url = f"{COINGLASS_BASE}/futures/top-long-short-position-ratio/history"
        params = {
            "symbol": f"{self.symbol}USDT",
            "interval": "5m",
            "limit": "1",
            "exchange": "Binance",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            body = await resp.json()

        if not self._is_success(body):
            self._log.debug("coinglass_enhanced.top_pos_failed", code=body.get("code"), msg=body.get("msg", "")[:60])
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        # v4 API field names (verified)
        long_pct = float(latest.get("top_position_long_percent",
                        latest.get("longAccount", 50.0)))
        short_pct = float(latest.get("top_position_short_percent",
                         latest.get("shortAccount", 50.0)))
        ratio = float(latest.get("top_position_long_short_ratio",
                     latest.get("longShortRatio", 1.0)))

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
        """Fetch aggregated taker buy/sell volume across exchanges."""
        url = f"{COINGLASS_BASE}/futures/aggregated-taker-buy-sell-volume/history"
        params = {
            "symbol": self.symbol,
            "interval": "5m",
            "limit": "1",
            "exchange_list": "Binance,OKX,Bybit,Bitget",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            body = await resp.json()

        if not self._is_success(body):
            self._log.debug("coinglass_enhanced.taker_failed", code=body.get("code"), msg=body.get("msg", "")[:60])
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        # v4 API field names (verified)
        buy_vol = float(latest.get("aggregated_buy_volume_usd", 0))
        sell_vol = float(latest.get("aggregated_sell_volume_usd", 0))

        self.snapshot.taker_buy_volume_1m = buy_vol
        self.snapshot.taker_sell_volume_1m = sell_vol

        self._log.debug(
            "coinglass_enhanced.taker_volume",
            buy_vol_usd=f"${buy_vol:,.0f}",
            sell_vol_usd=f"${sell_vol:,.0f}",
            ratio=f"{buy_vol / sell_vol:.2f}" if sell_vol > 0 else "inf",
        )

    async def _fetch_funding(self, session: aiohttp.ClientSession) -> None:
        """Fetch latest funding rate from Binance (8h OHLC candles)."""
        url = f"{COINGLASS_BASE}/futures/funding-rate/history"
        params = {
            "symbol": f"{self.symbol}USDT",
            "exchange": "Binance",
            "interval": "8h",
            "limit": "1",
        }

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            body = await resp.json()

        if not self._is_success(body):
            self._log.debug("coinglass_enhanced.funding_failed", code=body.get("code"), msg=body.get("msg", "")[:60])
            return

        data = body.get("data", [])
        if not data:
            return

        latest = data[-1] if isinstance(data, list) else data
        # v4 returns OHLC — use close for latest funding rate
        rate = float(latest.get("close",
                    latest.get("fundingRate",
                    latest.get("rate", 0))))
        self.snapshot.funding_rate = rate
        # Annualise: rate × 3 (8h periods/day) × 365
        self.snapshot.funding_rate_annual = rate * 3 * 365

        self._log.debug(
            "coinglass_enhanced.funding_rate",
            rate=f"{rate:.6f}",
            annual_pct=f"{rate * 3 * 365 * 100:.2f}%",
        )
