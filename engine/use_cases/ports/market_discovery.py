"""Application port: MarketDiscoveryPort.

Resolves an (asset, timeframe, window_ts) triple to a WindowMarket
(condition_id + up/down CLOB token IDs) via Polymarket Gamma API.
"""
from __future__ import annotations

import abc
from typing import Optional

from engine.domain.value_objects import Asset, Timeframe, WindowMarket


class MarketDiscoveryPort(abc.ABC):
    """Abstract Polymarket window market lookup."""

    @abc.abstractmethod
    async def find_window_market(
        self, asset: Asset, tf: Timeframe, window_ts: int
    ) -> Optional[WindowMarket]:
        """Return WindowMarket for the (asset, tf, window_ts) triple, or None."""
        ...
