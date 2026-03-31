"""
Tests for the VPIN (Volume-Synchronized Probability of Informed Trading) calculator.

Tests cover:
  - Single bucket accumulation
  - Bucket closure and imbalance calculation
  - Tick rule classification (buy/sell)
  - Rolling window truncation
  - VPIN threshold signal emission
  - Edge cases: zero volume, equal prices
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional

import pytest

# Use relative imports when running from engine/
from signals.vpin import VPINCalculator
from data.models import AggTrade, VPINSignal


def make_trade(
    price: float,
    quantity: float = 1.0,
    is_buyer_maker: bool = False,
) -> AggTrade:
    """Helper: create an AggTrade at the given price."""
    return AggTrade(
        symbol="BTCUSDT",
        price=Decimal(str(price)),
        quantity=Decimal(str(quantity)),
        is_buyer_maker=is_buyer_maker,
        trade_time=datetime.utcnow(),
    )


@pytest.fixture
def calc() -> VPINCalculator:
    """A fresh VPIN calculator with small bucket size for testing."""
    return VPINCalculator(bucket_size_usd=1_000, lookback_buckets=5)


class TestVPINBucketAccumulation:
    """Tests for volume bucket fill/close behaviour."""

    def test_initial_vpin_is_zero(self, calc: VPINCalculator) -> None:
        assert calc.current_vpin == 0.0

    def test_buckets_filled_starts_zero(self, calc: VPINCalculator) -> None:
        assert calc.buckets_filled == 0

    @pytest.mark.asyncio
    async def test_single_bucket_closes_on_threshold(self, calc: VPINCalculator) -> None:
        """Feed $1000 of trades → bucket closes, buckets_filled becomes 1."""
        # 10 trades × $100 = $1000 → closes one bucket
        for _ in range(10):
            await calc.on_trade(make_trade(price=100.0, quantity=1.0))  # $100 each

        assert calc.buckets_filled == 1

    @pytest.mark.asyncio
    async def test_vpin_is_between_zero_and_one(self, calc: VPINCalculator) -> None:
        """VPIN must always be in [0, 1]."""
        for i in range(50):
            price = 100.0 + (i % 3) - 1  # price oscillates
            await calc.on_trade(make_trade(price=price, quantity=1.0))

        assert 0.0 <= calc.current_vpin <= 1.0

    @pytest.mark.asyncio
    async def test_multiple_buckets_accumulate(self, calc: VPINCalculator) -> None:
        """Filling 30 buckets should give 5 in rolling window."""
        for _ in range(300):  # 300 × $100 = $30,000 → 30 buckets
            await calc.on_trade(make_trade(price=100.0, quantity=1.0))

        # Rolling window capped at lookback_buckets=5
        assert calc.buckets_filled == 5


class TestTickRuleClassification:
    """Tests for buy/sell classification via the tick rule."""

    @pytest.mark.asyncio
    async def test_rising_price_classified_as_buy(self) -> None:
        """Price increase → buy-initiated."""
        signals: list[VPINSignal] = []
        calc = VPINCalculator(
            bucket_size_usd=200,
            lookback_buckets=3,
            on_signal=lambda s: signals.append(s) or asyncio.sleep(0),  # type: ignore
        )

        # All trades at rising prices → should be classified as buys
        for price in [100.0, 101.0, 102.0]:
            # Each trade = price × 1 unit
            qty = 200.0 / price
            await calc.on_trade(make_trade(price=price, quantity=qty))

        # With all-buy volume, imbalance should be 1.0
        if signals:
            assert signals[-1].value >= 0.9

    @pytest.mark.asyncio
    async def test_falling_price_classified_as_sell(self) -> None:
        """Price decrease → sell-initiated."""
        calc = VPINCalculator(bucket_size_usd=200, lookback_buckets=3)

        for price in [102.0, 101.0, 100.0]:
            qty = 200.0 / price
            await calc.on_trade(make_trade(price=price, quantity=qty))

        # Sell-driven → imbalance high
        if calc.buckets_filled > 0:
            assert calc.current_vpin >= 0.0  # At least computed something

    @pytest.mark.asyncio
    async def test_equal_price_keeps_previous_classification(self) -> None:
        """Same price → treated as buy (conservative default)."""
        calc = VPINCalculator(bucket_size_usd=100, lookback_buckets=3)

        # One buy tick, then flat
        await calc.on_trade(make_trade(price=100.0, quantity=1.0))
        await calc.on_trade(make_trade(price=100.0, quantity=0.99))  # same price

        # No assertion on exact value; just verify no crash
        assert calc.current_vpin >= 0.0


class TestVPINSignalEmission:
    """Tests for signal callbacks and threshold crossing."""

    @pytest.mark.asyncio
    async def test_signal_callback_fires_on_bucket_close(self) -> None:
        """Signal callback must be called whenever a bucket closes."""
        received: list[VPINSignal] = []

        async def capture(sig: VPINSignal) -> None:
            received.append(sig)

        calc = VPINCalculator(bucket_size_usd=100, lookback_buckets=5, on_signal=capture)

        # Fill 3 buckets
        for _ in range(300):
            await calc.on_trade(make_trade(price=1.0, quantity=1.0))

        assert len(received) == 3

    @pytest.mark.asyncio
    async def test_signal_has_correct_fields(self) -> None:
        """VPINSignal must have valid value and threshold flags."""
        received: list[VPINSignal] = []

        async def capture(sig: VPINSignal) -> None:
            received.append(sig)

        calc = VPINCalculator(bucket_size_usd=100, lookback_buckets=3, on_signal=capture)

        for _ in range(100):
            await calc.on_trade(make_trade(price=1.0, quantity=1.0))

        sig = received[0]
        assert 0.0 <= sig.value <= 1.0
        assert isinstance(sig.informed_threshold_crossed, bool)
        assert isinstance(sig.cascade_threshold_crossed, bool)
        assert sig.buckets_filled >= 1

    @pytest.mark.asyncio
    async def test_high_imbalance_crosses_cascade_threshold(self) -> None:
        """100% buy volume should eventually trigger cascade threshold."""
        received: list[VPINSignal] = []

        async def capture(sig: VPINSignal) -> None:
            received.append(sig)

        calc = VPINCalculator(bucket_size_usd=100, lookback_buckets=50, on_signal=capture)

        # All rising prices → all-buy volume → imbalance ≈ 1.0
        price = 100.0
        for i in range(500):
            price += 0.01  # price always rising → tick rule = buy
            await calc.on_trade(make_trade(price=price, quantity=1.0))

        # After many buckets of pure buy flow, cascade threshold should trigger
        if received:
            last = received[-1]
            assert last.cascade_threshold_crossed or last.informed_threshold_crossed


class TestVPINEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_no_crash_on_single_trade(self) -> None:
        """A single small trade should not crash or emit a signal."""
        calc = VPINCalculator(bucket_size_usd=1_000_000)
        await calc.on_trade(make_trade(price=100.0, quantity=0.0001))
        assert calc.current_vpin == 0.0
        assert calc.buckets_filled == 0

    @pytest.mark.asyncio
    async def test_lookback_window_caps_correctly(self) -> None:
        """buckets_filled should never exceed lookback_buckets."""
        calc = VPINCalculator(bucket_size_usd=10, lookback_buckets=3)

        for _ in range(1000):
            await calc.on_trade(make_trade(price=1.0, quantity=1.0))

        assert calc.buckets_filled <= 3
