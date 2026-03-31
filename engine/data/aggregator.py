"""
Market Aggregator — Unified Market State

Collects data from all feeds and maintains a single consistent
MarketState object that strategies read from.

Features:
  - Tracks 5-minute rolling liquidation volume from Binance forceOrder events
  - Tracks btc_price_5m_ago for cascade detector (deque with timestamps)
  - stream() async generator yields MarketState on every update
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from typing import AsyncIterator, Optional
import structlog

from data.models import (
    AggTrade,
    ForcedLiquidation,
    LiquidationVolume,
    OpenInterestSnapshot,
    ChainlinkPrice,
    PolymarketOrderBook,
    MarketState,
    VPINSignal,
    CascadeSignal,
    ArbOpportunity,
)

log = structlog.get_logger(__name__)

LIQ_WINDOW_SECONDS = 300       # 5 minutes
PRICE_HISTORY_SECONDS = 310    # Slightly more than 5m to ensure we always have a value


class MarketAggregator:
    """
    Central hub for all incoming market data.

    Feeds push data in; strategies pull the latest MarketState via stream().
    Thread-safe via asyncio.Lock.

    The stream() async generator yields a fresh MarketState snapshot on every
    update, allowing the orchestrator to fan out to strategies with:

        async for state in aggregator.stream():
            await strategy.on_market_state(state)
    """

    def __init__(self) -> None:
        self._state = MarketState()
        self._lock = asyncio.Lock()
        self._update_event = asyncio.Event()

        # 5-minute rolling liquidation volume: deque of (timestamp, usd_value)
        self._liq_events: deque[tuple[datetime, Decimal]] = deque()

        # BTC price history for 5m-ago lookup: deque of (timestamp, price)
        self._price_history: deque[tuple[datetime, Decimal]] = deque()

    # ─── Feed Handlers ────────────────────────────────────────────────────────

    async def on_agg_trade(self, trade: AggTrade) -> None:
        """Handle incoming Binance aggTrade — updates BTC price and price history."""
        async with self._lock:
            now = datetime.utcnow()
            self._state.btc_price = trade.price
            self._state.last_updated = now

            # Append to price history
            self._price_history.append((now, trade.price))

            # Prune old entries
            cutoff = now - timedelta(seconds=PRICE_HISTORY_SECONDS)
            while self._price_history and self._price_history[0][0] < cutoff:
                self._price_history.popleft()

            # Compute btc_price_5m_ago: oldest entry still within ~5min window
            five_min_ago = now - timedelta(seconds=300)
            self._state.btc_price_5m_ago = self._find_price_near(five_min_ago)

        self._update_event.set()

    async def on_liquidation(self, liq: ForcedLiquidation) -> None:
        """
        Handle Binance forceOrder event.

        Accumulates liquidation notional in a 5-minute rolling window.
        """
        notional = liq.price * liq.quantity
        async with self._lock:
            now = datetime.utcnow()
            self._liq_events.append((now, notional))

            # Prune entries older than the window
            cutoff = now - timedelta(seconds=LIQ_WINDOW_SECONDS)
            while self._liq_events and self._liq_events[0][0] < cutoff:
                self._liq_events.popleft()

            rolling_liq = sum(v for _, v in self._liq_events)
            self._state.liq_volume_5m_usd = rolling_liq
            self._state.last_updated = now

            log.debug(
                "aggregator.liquidation",
                side=liq.side,
                notional=str(notional),
                rolling_5m=str(rolling_liq),
            )
        self._update_event.set()

    async def on_liquidation_volume(self, liq: LiquidationVolume) -> None:
        """
        Handle LiquidationVolume event from CoinGlass (longer-term signal).

        Updates the liq_volume_usd field (used by cascade detector).
        """
        async with self._lock:
            self._state.liq_volume_usd = liq.liq_volume_usd
            self._state.last_updated = datetime.utcnow()
        self._update_event.set()

    async def on_open_interest(self, oi: OpenInterestSnapshot) -> None:
        """Handle OI snapshot from CoinGlass."""
        async with self._lock:
            self._state.open_interest_usd = oi.open_interest_usd
            self._state.oi_delta_pct = oi.open_interest_delta_pct
            self._state.last_updated = datetime.utcnow()
        self._update_event.set()

    async def on_chainlink_price(self, price: ChainlinkPrice) -> None:
        """Handle Chainlink oracle price update."""
        async with self._lock:
            self._state.chainlink_price = price.price
            self._state.last_updated = datetime.utcnow()
        self._update_event.set()

    async def on_polymarket_book(self, book: PolymarketOrderBook) -> None:
        """Handle Polymarket order book update."""
        log.debug("aggregator.polymarket_book", market=book.market_slug)
        # Arb scanner processes books separately; this triggers a re-scan
        self._update_event.set()

    async def on_vpin_signal(self, vpin: VPINSignal) -> None:
        """Handle updated VPIN value from the signal processor."""
        async with self._lock:
            self._state.vpin = vpin
        self._update_event.set()

    async def on_cascade_signal(self, cascade: CascadeSignal) -> None:
        """Handle cascade FSM state update."""
        async with self._lock:
            self._state.cascade = cascade
        self._update_event.set()

    async def on_arb_opportunities(self, opps: list[ArbOpportunity]) -> None:
        """Handle refreshed arb opportunity list."""
        async with self._lock:
            self._state.arb_opportunities = opps
        self._update_event.set()

    # ─── Stream Interface ─────────────────────────────────────────────────────

    async def stream(self) -> AsyncIterator[MarketState]:
        """
        Async generator that yields a fresh MarketState snapshot on every update.

        The orchestrator consumes this to fan out state to all strategies:

            async for state in aggregator.stream():
                await strategy.on_market_state(state)
        """
        while True:
            await self._update_event.wait()
            self._update_event.clear()
            yield await self.get_state()

    # ─── State Access ─────────────────────────────────────────────────────────

    async def get_state(self) -> MarketState:
        """Return a deep copy snapshot of the current market state."""
        async with self._lock:
            return self._state.model_copy(deep=True)

    # ─── Internal Helpers ─────────────────────────────────────────────────────

    def _find_price_near(self, target_time: datetime) -> Optional[Decimal]:
        """
        Return the BTC price closest to target_time from the history deque.

        Returns None if history is empty.
        """
        if not self._price_history:
            return None

        best: Optional[Decimal] = None
        best_delta = timedelta.max

        for ts, price in self._price_history:
            delta = abs(ts - target_time)
            if delta < best_delta:
                best_delta = delta
                best = price

        return best
