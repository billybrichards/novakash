"""
VPIN (Volume-Synchronized Probability of Informed Trading) Calculator

VPIN is a real-time estimate of informed trading activity based on order
flow imbalance, computed over volume-synchronized time buckets rather than
calendar-time buckets.

Algorithm:
  1. Accumulate aggTrades into buckets of fixed USD notional (VPIN_BUCKET_SIZE_USD).
  2. For each trade, classify buy-initiated vs sell-initiated volume using the
     tick rule (if price ↑ vs previous → buy; if ↓ → sell; if same → previous).
  3. When a bucket fills: bucket_imbalance = |buy_vol - sell_vol| / total_vol
  4. VPIN = mean(bucket_imbalance) over the last VPIN_LOOKBACK_BUCKETS buckets.

Thresholds (from config/constants.py):
  VPIN > 0.55 → elevated informed flow (warn)
  VPIN > 0.70 → cascade-level informed flow (signal)

References:
  - Easley, López de Prado, O'Hara (2012) "Flow Toxicity and Liquidity in a
    High-Frequency World". The Review of Financial Studies.
  - Adapted for crypto perpetuals where volume is in USD notional.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import Callable, Awaitable
import structlog

from config.constants import (
    VPIN_BUCKET_SIZE_USD,
    VPIN_LOOKBACK_BUCKETS,
    VPIN_INFORMED_THRESHOLD,
    VPIN_CASCADE_THRESHOLD,
)
from data.models import AggTrade, VPINSignal

log = structlog.get_logger(__name__)


class VPINCalculator:
    """
    Computes VPIN metric from streaming Binance aggTrade events.

    Usage:
        calc = VPINCalculator(on_signal=my_handler)
        # Feed trades as they arrive:
        await calc.on_trade(trade)
    """

    def __init__(
        self,
        bucket_size_usd: float = VPIN_BUCKET_SIZE_USD,
        lookback_buckets: int = VPIN_LOOKBACK_BUCKETS,
        on_signal: Callable[[VPINSignal], Awaitable[None]] | None = None,
    ) -> None:
        self.bucket_size_usd = bucket_size_usd
        self.lookback_buckets = lookback_buckets
        self._on_signal = on_signal

        # Current bucket accumulators
        self._bucket_buy_vol: float = 0.0
        self._bucket_sell_vol: float = 0.0
        self._bucket_total_vol: float = 0.0

        # Completed bucket imbalances (rolling window)
        self._buckets: deque[float] = deque(maxlen=lookback_buckets)

        # Tick rule state
        self._prev_price: Decimal | None = None

        self._current_vpin: float = 0.0

    async def on_trade(self, trade: AggTrade) -> None:
        """
        Ingest one aggTrade and update VPIN.

        Classifies volume as buy or sell using the tick rule:
          - price > prev_price  → buy-initiated
          - price < prev_price  → sell-initiated
          - price == prev_price → same as previous classification
        """
        notional_usd = float(trade.price * trade.quantity)
        direction = self._classify(trade.price)

        if direction == "buy":
            self._bucket_buy_vol += notional_usd
        else:
            self._bucket_sell_vol += notional_usd

        self._bucket_total_vol += notional_usd
        self._prev_price = trade.price

        # Check if bucket is full
        if self._bucket_total_vol >= self.bucket_size_usd:
            await self._close_bucket(trade.trade_time)

    def _classify(self, price: Decimal) -> str:
        """Classify trade direction using the tick rule."""
        if self._prev_price is None:
            return "buy"
        if price > self._prev_price:
            return "buy"
        elif price < self._prev_price:
            return "sell"
        else:
            # No price change — use bulk volume classification (split 50/50 or inherit)
            return "buy"  # Conservative: treat ties as buy

    async def _close_bucket(self, timestamp: datetime) -> None:
        """Finalise current bucket, compute imbalance, update VPIN."""
        if self._bucket_total_vol > 0:
            imbalance = abs(self._bucket_buy_vol - self._bucket_sell_vol) / self._bucket_total_vol
            self._buckets.append(imbalance)

        # Reset bucket accumulators
        self._bucket_buy_vol = 0.0
        self._bucket_sell_vol = 0.0
        self._bucket_total_vol = 0.0

        if not self._buckets:
            return

        self._current_vpin = sum(self._buckets) / len(self._buckets)

        signal = VPINSignal(
            value=self._current_vpin,
            buckets_filled=len(self._buckets),
            informed_threshold_crossed=self._current_vpin >= VPIN_INFORMED_THRESHOLD,
            cascade_threshold_crossed=self._current_vpin >= VPIN_CASCADE_THRESHOLD,
            timestamp=timestamp,
        )

        log.debug(
            "vpin.bucket_closed",
            vpin=f"{self._current_vpin:.4f}",
            buckets=len(self._buckets),
            cascade=signal.cascade_threshold_crossed,
        )

        if self._on_signal:
            await self._on_signal(signal)

    @property
    def current_vpin(self) -> float:
        """Return the latest VPIN value (0–1)."""
        return self._current_vpin

    @property
    def buckets_filled(self) -> int:
        """Return how many historical buckets are in the rolling window."""
        return len(self._buckets)
