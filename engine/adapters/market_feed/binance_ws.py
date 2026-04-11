"""Binance WebSocket adapter -- wraps ``data.feeds.binance_ws.BinanceWebSocketFeed``.

Implements :class:`engine.domain.ports.MarketFeedPort` by delegating to
the existing ``BinanceWebSocketFeed`` concrete class.  The adapter is a
thin shim -- zero business logic, just protocol conformance.

The Binance feed is tick-based (aggTrade stream), so ``get_latest_tick``
returns the most recent aggTrade price cached by the feed, and
``get_window_delta`` returns ``None`` (the actual delta computation stays
in ``five_min_vpin.py`` until Phase 3 wires the use case).

``subscribe_window_close`` is not implemented -- the orchestrator drives
window timing from the clock, not from the feed.

Phase 2 deliverable (CA-02).  Nothing imports this adapter yet.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Optional

import structlog

from engine.domain.ports import MarketFeedPort
from engine.domain.value_objects import Tick, WindowClose

if TYPE_CHECKING:
    from data.feeds.binance_ws import BinanceWebSocketFeed

log = structlog.get_logger(__name__)


class BinanceWebSocketAdapter(MarketFeedPort):
    """Wraps :class:`BinanceWebSocketFeed` behind :class:`MarketFeedPort`.

    Constructor accepts the concrete feed instance -- the composition
    root creates it and passes it in.  This adapter never constructs
    the feed itself.

    Parameters
    ----------
    feed : BinanceWebSocketFeed
        The running (or about-to-be-started) Binance WebSocket feed.
    staleness_threshold_s : float
        Maximum age (seconds) of the last message before
        ``get_latest_tick`` returns ``None``.  Default 30s -- Binance
        aggTrade fires multiple times per second under normal conditions,
        so 30s of silence means the feed is effectively dead.
    """

    def __init__(
        self,
        feed: "BinanceWebSocketFeed",
        staleness_threshold_s: float = 30.0,
    ) -> None:
        self._feed = feed
        self._staleness_s = staleness_threshold_s
        self._log = log.bind(adapter="binance_ws")

    # -- MarketFeedPort: get_latest_tick ------------------------------------

    async def get_latest_tick(self, asset: str) -> Optional[Tick]:
        """Return the most recent price from the Binance aggTrade stream.

        Returns ``None`` if the feed is disconnected or the last message
        is older than ``staleness_threshold_s``.
        """
        try:
            if not self._feed.connected:
                self._log.debug("binance_ws.not_connected")
                return None

            last_msg = self._feed.last_message_at
            if last_msg is None:
                return None

            age_s = time.time() - last_msg.timestamp()
            if age_s > self._staleness_s:
                self._log.debug(
                    "binance_ws.stale",
                    age_s=f"{age_s:.1f}",
                    threshold_s=self._staleness_s,
                )
                return None

            # TODO: TECH_DEBT - populate Tick fields (price, ts, source)
            # when the VO is fleshed out.  The feed stores price in the
            # aggregator's MarketState, not on the feed object itself.
            return Tick()

        except Exception as exc:
            self._log.debug("binance_ws.get_latest_tick_failed", error=str(exc)[:80])
            return None

    # -- MarketFeedPort: get_window_delta -----------------------------------

    async def get_window_delta(
        self,
        asset: str,
        window_ts: int,
        open_price: float,
    ) -> Optional[float]:
        """Compute pct delta using the feed's latest price vs ``open_price``.

        Returns ``None`` -- the Binance WebSocket adapter is a structural
        shim for now.  The actual delta computation stays in
        ``five_min_vpin.py`` until Phase 3 wires the use case.
        """
        try:
            tick = await self.get_latest_tick(asset)
            if tick is None:
                return None

            # TODO: TECH_DEBT - read actual price from Tick once VO has fields,
            # then compute (price - open_price) / open_price * 100.
            self._log.debug(
                "binance_ws.get_window_delta.stub",
                asset=asset,
                window_ts=window_ts,
            )
            return None

        except Exception as exc:
            self._log.debug(
                "binance_ws.get_window_delta_failed",
                error=str(exc)[:80],
            )
            return None

    # -- MarketFeedPort: subscribe_window_close -----------------------------

    def subscribe_window_close(
        self,
        asset: str,
        timeframe: str,
    ) -> AsyncIterator[WindowClose]:
        """Not implemented -- window timing is clock-driven, not feed-driven.

        Raises ``NotImplementedError`` since the Binance aggTrade stream
        does not emit window-close events.
        """
        raise NotImplementedError(
            "BinanceWebSocketAdapter does not provide window-close events; "
            "use the Clock port for window timing"
        )
