"""
VPIN (Volume-synchronized Probability of Informed Trading) Calculator.

Uses Easley et al. bulk-volume classification with fixed-USD-volume buckets.
Buy/sell classification is taken directly from AggTrade.is_buyer_maker:
  - is_buyer_maker=True  → the aggressor is the SELLER (taker sold)
  - is_buyer_maker=False → the aggressor is the BUYER  (taker bought)
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

import structlog

from config.runtime_config import runtime
from data.models import AggTrade, VPINSignal

log = structlog.get_logger(__name__)


class VPINCalculator:
    """
    Computes VPIN using fixed-USD-volume buckets.

    Each bucket accumulates trades until the total notional value reaches
    bucket_size_usd.  When a bucket is complete, the imbalance ratio
    |buy_vol - sell_vol| / total_vol is stored and VPIN is recalculated as
    the mean imbalance over the last lookback_buckets buckets.

    Parameters
    ----------
    bucket_size_usd:
        USD notional per bucket. Defaults to runtime.vpin_bucket_size_usd constant.
    lookback_buckets:
        Rolling window size. Defaults to runtime.vpin_lookback_buckets constant.
    on_signal:
        Optional async callback invoked with a :class:`VPINSignal` each time
        a bucket completes.
    """

    def __init__(
        self,
        bucket_size_usd: float = runtime.vpin_bucket_size_usd,
        lookback_buckets: int = runtime.vpin_lookback_buckets,
        on_signal: Optional[Callable[[VPINSignal], Awaitable[None]]] = None,
    ) -> None:
        self._bucket_size_usd = bucket_size_usd
        self._lookback_buckets = lookback_buckets
        self._on_signal = on_signal

        # Completed bucket imbalances: deque of floats in [0, 1]
        self._bucket_imbalances: deque[float] = deque(maxlen=lookback_buckets)

        # Current (in-progress) bucket accumulators
        self._bucket_buy_usd: float = 0.0
        self._bucket_sell_usd: float = 0.0
        self._bucket_total_usd: float = 0.0

        # Running VPIN value
        self._current_vpin: float = 0.0

        self._log = log.bind(component="VPINCalculator")
        self._log.info(
            "initialised",
            bucket_size_usd=bucket_size_usd,
            lookback_buckets=lookback_buckets,
            informed_threshold=runtime.vpin_informed_threshold,
            cascade_threshold=runtime.vpin_cascade_threshold,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def on_trade(self, trade: AggTrade) -> None:
        """
        Ingest a single aggregated trade.

        Parameters
        ----------
        trade:
            An :class:`AggTrade` with price, quantity, and is_buyer_maker.
        """
        notional = float(trade.price * trade.quantity)

        # Classify aggressor side
        # is_buyer_maker=True  → market sell (maker is buyer → taker is seller)
        # is_buyer_maker=False → market buy  (maker is seller → taker is buyer)
        if trade.is_buyer_maker:
            self._bucket_sell_usd += notional
        else:
            self._bucket_buy_usd += notional

        self._bucket_total_usd += notional

        # Check if bucket is complete (may span multiple completions for large trades)
        while self._bucket_total_usd >= self._bucket_size_usd:
            await self._close_bucket()

    @property
    def current_vpin(self) -> float:
        """Current VPIN estimate in [0, 1].  0.0 until first bucket completes."""
        return self._current_vpin

    @property
    def current_bucket_fill_pct(self) -> float:
        """Fraction of the current (open) bucket that has been filled, in [0, 1]."""
        if self._bucket_size_usd <= 0:
            return 0.0
        return min(self._bucket_total_usd / self._bucket_size_usd, 1.0)

    @property
    def buckets_filled(self) -> int:
        """Total number of completed buckets (capped at lookback_buckets)."""
        return len(self._bucket_imbalances)

    async def warm_start(self, db_pool) -> int:
        """
        Pre-fill VPIN buckets from recent ticks_binance data.
        
        Called on engine startup to avoid cold-start period where VPIN = 0.
        Replays last 30 minutes of trades through the bucket algorithm.
        
        Returns number of ticks replayed.
        """
        if not db_pool:
            return 0
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT price, quantity, is_buyer_maker
                    FROM ticks_binance
                    WHERE ts > NOW() - INTERVAL '30 minutes'
                      AND symbol = 'BTCUSDT'
                    ORDER BY ts ASC
                """)
            
            if not rows:
                self._log.info("vpin.warm_start.no_data")
                return 0
            
            count = 0
            for row in rows:
                price = float(row["price"])
                qty = float(row["quantity"])
                notional = price * qty
                
                if row["is_buyer_maker"]:
                    self._bucket_sell_usd += notional
                else:
                    self._bucket_buy_usd += notional
                
                self._bucket_total_usd += notional
                
                while self._bucket_total_usd >= self._bucket_size_usd:
                    # Close bucket without triggering signal callbacks
                    total = self._bucket_buy_usd + self._bucket_sell_usd
                    imbalance = abs(self._bucket_buy_usd - self._bucket_sell_usd) / total if total > 0 else 0.0
                    self._bucket_imbalances.append(imbalance)
                    
                    overflow = self._bucket_total_usd - self._bucket_size_usd
                    self._bucket_buy_usd = overflow * (self._bucket_buy_usd / self._bucket_total_usd) if self._bucket_total_usd > 0 else 0
                    self._bucket_sell_usd = overflow * (self._bucket_sell_usd / self._bucket_total_usd) if self._bucket_total_usd > 0 else 0
                    self._bucket_total_usd = overflow
                    
                    if self._bucket_imbalances:
                        self._current_vpin = sum(self._bucket_imbalances) / len(self._bucket_imbalances)
                
                count += 1
            
            self._log.info(
                "vpin.warm_start.complete",
                ticks_replayed=count,
                buckets_filled=len(self._bucket_imbalances),
                vpin=f"{self._current_vpin:.4f}",
            )
            return count
        except Exception as exc:
            self._log.warning("vpin.warm_start.failed", error=str(exc)[:100])
            return 0

    def get_history(self, n: int) -> list[float]:
        """
        Return the last *n* completed VPIN bucket imbalance readings.

        Parameters
        ----------
        n:
            Number of historical readings to return.  Clamped to available data.

        Returns
        -------
        list[float]:
            Most-recent readings, oldest first.
        """
        data = list(self._bucket_imbalances)
        return data[-n:] if n < len(data) else data

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _close_bucket(self) -> None:
        """Finalise the current bucket, compute VPIN, and emit a signal."""
        total = self._bucket_total_usd
        buy = self._bucket_buy_usd
        sell = self._bucket_sell_usd

        # Imbalance ratio for this bucket
        imbalance = abs(buy - sell) / total if total > 0 else 0.0
        self._bucket_imbalances.append(imbalance)

        # Recompute VPIN as rolling mean of imbalances
        self._current_vpin = sum(self._bucket_imbalances) / len(self._bucket_imbalances)

        self._log.debug(
            "bucket_closed",
            bucket_buy_usd=round(buy, 2),
            bucket_sell_usd=round(sell, 2),
            imbalance=round(imbalance, 4),
            vpin=round(self._current_vpin, 4),
            buckets_filled=self.buckets_filled,
        )

        # Reset bucket — carry over any excess notional proportionally
        excess = total - self._bucket_size_usd
        if excess > 0 and total > 0:
            buy_ratio = buy / total
            self._bucket_buy_usd = excess * buy_ratio
            self._bucket_sell_usd = excess * (1.0 - buy_ratio)
            self._bucket_total_usd = excess
        else:
            self._bucket_buy_usd = 0.0
            self._bucket_sell_usd = 0.0
            self._bucket_total_usd = 0.0

        # Emit signal with correct VPINSignal field names
        if self._on_signal is not None:
            signal = VPINSignal(
                value=self._current_vpin,
                buckets_filled=self.buckets_filled,
                informed_threshold_crossed=self._current_vpin >= runtime.vpin_informed_threshold,
                cascade_threshold_crossed=self._current_vpin >= runtime.vpin_cascade_threshold,
                timestamp=datetime.now(tz=timezone.utc),
            )
            try:
                await self._on_signal(signal)
            except Exception:
                self._log.exception("on_signal callback raised", vpin=self._current_vpin)
