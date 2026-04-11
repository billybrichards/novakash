"""Tiingo REST 5-minute candle adapter.

Implements :class:`engine.domain.ports.MarketFeedPort` for Tiingo crypto
5-minute candles.  Extracted from the inline HTTP block in
``engine/strategies/five_min_vpin.py`` (lines 426-473, CA-02).

Security fix: reads ``TIINGO_API_KEY`` from environment instead of the
previously hardcoded string literal.

The adapter is intentionally thin -- same HTTP logic as the inline block,
same response parsing, same error-swallowing contract from the port
docstring (return ``None`` on any failure).

Audit: CA-02 (Tiingo adapter extraction + hardcoded key removal).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import structlog

from engine.domain.ports import MarketFeedPort

log = structlog.get_logger(__name__)


class TiingoRestAdapter(MarketFeedPort):
    """Tiingo REST crypto candle adapter.

    Fetches 5-minute OHLCV candles from the Tiingo crypto endpoint and
    computes the open-to-close delta for a given window.

    Constructor accepts an explicit ``api_key`` or falls back to the
    ``TIINGO_API_KEY`` environment variable.  Raises ``ValueError`` at
    construction time if neither is available -- fail fast, not at
    first request.
    """

    BASE_URL = "https://api.tiingo.com/tiingo/crypto/prices"
    DEFAULT_TIMEOUT_S = 3.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        resolved_key = api_key or os.environ.get("TIINGO_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "TiingoRestAdapter requires TIINGO_API_KEY env var or "
                "explicit api_key parameter"
            )
        self._api_key = resolved_key
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._log = log.bind(adapter="tiingo_rest")

    # -- MarketFeedPort: get_latest_tick ------------------------------------

    async def get_latest_tick(self, asset: str) -> None:
        """Not implemented -- Tiingo REST is candle-based, not tick-based.

        Returns ``None`` unconditionally per the port contract (a miss is
        not an error).
        """
        return None

    # -- MarketFeedPort: get_window_delta -----------------------------------

    async def get_window_delta(
        self,
        asset: str,
        window_ts: int,
        open_price: float,
    ) -> Optional[float]:
        """Fetch the 5-minute candle for ``window_ts`` and compute pct delta.

        Returns ``None`` on any failure (network, timeout, bad response,
        missing data) -- callers fall back to their next price source.
        """
        ticker = f"{asset.lower()}usd"
        start = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        end = datetime.fromtimestamp(window_ts + 300, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        url = (
            f"{self.BASE_URL}"
            f"?tickers={ticker}"
            f"&startDate={start}"
            f"&endDate={end}"
            f"&resampleFreq=5min"
            f"&token={self._api_key}"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=self._timeout) as resp:
                    if resp.status != 200:
                        self._log.debug(
                            "tiingo.non_200",
                            status=resp.status,
                            asset=asset,
                        )
                        return None

                    data = await resp.json()

                    if not data or not isinstance(data, list) or len(data) == 0:
                        return None

                    price_data = data[0].get("priceData", [])
                    if not price_data or len(price_data) == 0:
                        return None

                    candle_open = float(price_data[0].get("open", 0) or 0) or None
                    candle_close = float(price_data[-1].get("close", 0) or 0) or None

                    if candle_open and candle_close and candle_open > 0:
                        delta = (candle_close - candle_open) / candle_open * 100
                        self._log.info(
                            "tiingo.candle_fetched",
                            asset=asset,
                            open=f"${candle_open:,.2f}",
                            close=f"${candle_close:,.2f}",
                            delta=f"{delta:+.4f}%",
                            candles=len(price_data),
                        )
                        return delta

                    return None

        except Exception as exc:
            self._log.debug(
                "tiingo.candle_fetch_failed",
                error=str(exc)[:80],
            )
            return None

    # -- MarketFeedPort: subscribe_window_close -----------------------------

    def subscribe_window_close(
        self,
        asset: str,
        timeframe: str,
    ) -> AsyncIterator:
        """Not implemented -- Tiingo REST is poll-based, not streaming.

        Raises ``NotImplementedError`` since this method should never be
        called on the REST adapter.
        """
        raise NotImplementedError(
            "TiingoRestAdapter is poll-based; use a WebSocket feed for "
            "window-close subscriptions"
        )
