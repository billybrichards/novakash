"""
Market Aggregator — Unified Market State

Collects data from all feeds and maintains a single consistent
MarketState object that strategies read from.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional
import structlog

from data.models import (
    AggTrade,
    ForcedLiquidation,
    OpenInterestSnapshot,
    ChainlinkPrice,
    PolymarketOrderBook,
    MarketState,
    VPINSignal,
    CascadeSignal,
    ArbOpportunity,
)

log = structlog.get_logger(__name__)


class MarketAggregator:
    """
    Central hub for all incoming market data.

    Feeds push data in; strategies pull the latest MarketState.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self) -> None:
        self._state = MarketState()
        self._lock = asyncio.Lock()
        self._update_callbacks: list = []

    async def on_agg_trade(self, trade: AggTrade) -> None:
        """Handle incoming Binance aggTrade — updates BTC price."""
        async with self._lock:
            self._state.btc_price = trade.price
            self._state.last_updated = datetime.utcnow()
        await self._notify()

    async def on_liquidation(self, liq: ForcedLiquidation) -> None:
        """Handle Binance forceOrder event."""
        log.debug("aggregator.liquidation", side=liq.side, qty=str(liq.quantity))
        # Liquidation volume is aggregated in CoinGlass polling; this is real-time signal
        await self._notify()

    async def on_open_interest(self, oi: OpenInterestSnapshot) -> None:
        """Handle OI snapshot from CoinGlass."""
        async with self._lock:
            self._state.open_interest_usd = oi.open_interest_usd
            self._state.oi_delta_pct = oi.open_interest_delta_pct
            self._state.last_updated = datetime.utcnow()
        await self._notify()

    async def on_chainlink_price(self, price: ChainlinkPrice) -> None:
        """Handle Chainlink oracle price update."""
        async with self._lock:
            self._state.chainlink_price = price.price
            self._state.last_updated = datetime.utcnow()
        await self._notify()

    async def on_polymarket_book(self, book: PolymarketOrderBook) -> None:
        """Handle Polymarket order book update."""
        log.debug("aggregator.polymarket_book", market=book.market_slug)
        # Arb scanner processes books separately; this triggers a scan
        await self._notify()

    async def on_vpin_signal(self, vpin: VPINSignal) -> None:
        """Handle updated VPIN value from the signal processor."""
        async with self._lock:
            self._state.vpin = vpin
        await self._notify()

    async def on_cascade_signal(self, cascade: CascadeSignal) -> None:
        """Handle cascade FSM state update."""
        async with self._lock:
            self._state.cascade = cascade
        await self._notify()

    async def on_arb_opportunities(self, opps: list[ArbOpportunity]) -> None:
        """Handle refreshed arb opportunity list."""
        async with self._lock:
            self._state.arb_opportunities = opps
        await self._notify()

    async def get_state(self) -> MarketState:
        """Return a snapshot of the current market state."""
        async with self._lock:
            return self._state.model_copy(deep=True)

    def subscribe(self, callback) -> None:
        """Register a callback to be called on every state update."""
        self._update_callbacks.append(callback)

    async def _notify(self) -> None:
        """Fire all registered callbacks with the latest state."""
        if not self._update_callbacks:
            return
        state = await self.get_state()
        for cb in self._update_callbacks:
            try:
                await cb(state)
            except Exception as exc:
                log.error("aggregator.callback_error", error=str(exc))
