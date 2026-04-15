"""Application port: PriceGateway.

Per-asset current price + window candle lookup. Concrete implementations
route across ChainlinkFeed / TiingoFeed / BinanceWebSocketFeed.
"""
from __future__ import annotations

import abc
from typing import Optional

from engine.domain.value_objects import Asset, PriceCandle, Timeframe


class PriceGateway(abc.ABC):
    """Abstract per-asset price source."""

    @abc.abstractmethod
    async def get_current_price(self, asset: Asset) -> Optional[float]:
        """Latest spot price for asset, or None if unavailable."""
        ...

    @abc.abstractmethod
    async def get_window_candle(
        self, asset: Asset, window_ts: int, tf: Timeframe
    ) -> Optional[PriceCandle]:
        """Open + close price for a given window. None if unavailable."""
        ...
