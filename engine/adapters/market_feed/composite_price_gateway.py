"""Composite PriceGateway adapter.

Routes per-asset price lookup across Chainlink / Tiingo / Binance.
Moves inline aiohttp logic out of engine/use_cases/evaluate_window.py.

Routing:
    BTC → Binance spot latest_price (fastest, authoritative spot)
    ETH/SOL/XRP/DOGE/BNB → Chainlink latest_prices[symbol] primary
                           → DB tiingo latest tick fallback
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

import structlog

from domain.value_objects import Asset, PriceCandle, Timeframe
from use_cases.ports.price_gateway import PriceGateway

log = structlog.get_logger(__name__)

_TIINGO_URL = "https://api.tiingo.com/tiingo/crypto/prices"


class CompositePriceGateway(PriceGateway):
    """Multi-source price gateway.

    http_session_factory: zero-arg callable returning an aiohttp.ClientSession-
    compatible async context manager. Pass None to disable REST path (tests).
    """

    def __init__(
        self,
        chainlink_feed: Any,
        binance_spot_feed: Any,
        db: Any,
        tiingo_api_key: str,
        http_session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._chainlink = chainlink_feed
        self._binance = binance_spot_feed
        self._db = db
        self._tiingo_api_key = tiingo_api_key
        self._session_factory = http_session_factory

    async def get_current_price(self, asset: Asset) -> Optional[float]:
        if asset.symbol == "BTC":
            p = getattr(self._binance, "latest_price", None)
            if p:
                return float(p)
            # BTC also on Chainlink for fallback
            p2 = self._chainlink.latest_prices.get("BTC")
            if p2:
                return float(p2)
            try:
                return await self._db.get_latest_tiingo_price("BTC")
            except Exception:
                return None

        p = self._chainlink.latest_prices.get(asset.symbol)
        if p:
            return float(p)
        try:
            p2 = await self._db.get_latest_tiingo_price(asset.symbol)
            return float(p2) if p2 else None
        except Exception:
            return None

    async def get_window_candle(
        self, asset: Asset, window_ts: int, tf: Timeframe
    ) -> Optional[PriceCandle]:
        """Fetch (open, close) for the window via Tiingo REST. None if disabled."""
        if self._session_factory is None:
            return None

        ts_s = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        ts_e = datetime.fromtimestamp(
            window_ts + tf.duration_secs, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        resample = "15min" if tf.duration_secs == 900 else "5min"
        url = (
            f"{_TIINGO_URL}?tickers={asset.symbol.lower()}usd"
            f"&startDate={ts_s}&endDate={ts_e}"
            f"&resampleFreq={resample}&token={self._tiingo_api_key}"
        )

        try:
            import aiohttp  # lazy import — keeps test path fast

            async with self._session_factory() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=3.0)
                ) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
        except Exception as exc:
            log.warning("tiingo.candle_error", asset=asset.symbol, error=str(exc)[:200])
            return None

        if not isinstance(data, list) or not data:
            return None
        pd = data[0].get("priceData") or []
        if not pd:
            return None
        open_p = float(pd[0].get("open") or 0) or None
        close_p = float(pd[-1].get("close") or 0) or None
        if not (open_p and close_p and open_p > 0):
            return None
        return PriceCandle(open_p, close_p, source="tiingo_rest")
