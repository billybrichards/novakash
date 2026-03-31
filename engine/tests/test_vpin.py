"""
Tests for VPINCalculator.

Tests bucket filling, VPIN value computation, and signal emission.
"""

import pytest
import asyncio
from decimal import Decimal
from datetime import datetime
from signals.vpin import VPINCalculator
from data.models import AggTrade


def _make_trade(
    price: Decimal = Decimal("50000"),
    quantity: Decimal = Decimal("0.002"),
    is_buyer_maker: bool = False,
) -> AggTrade:
    return AggTrade(
        symbol="BTCUSDT",
        price=price,
        quantity=quantity,
        is_buyer_maker=is_buyer_maker,
        trade_time=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_bucket_fills_at_threshold():
    """Bucket should complete when cumulative volume reaches BUCKET_SIZE."""
    signals = []

    async def capture(sig):
        signals.append(sig)

    calc = VPINCalculator(bucket_size_usd=1000, lookback_buckets=5, on_signal=capture)

    # Feed 10 trades of $100 each (50000 * 0.002 = 100), alternating buy/sell
    for i in range(10):
        trade = AggTrade(
            symbol="BTCUSDT",
            price=Decimal("50000"),
            quantity=Decimal("0.002"),
            is_buyer_maker=(i % 2 == 0),
            trade_time=datetime.utcnow(),
        )
        await calc.on_trade(trade)

    assert len(signals) == 1  # One bucket completed
    assert 0 <= signals[0].value <= 1


@pytest.mark.asyncio
async def test_all_buys_high_vpin():
    """All buy-side trades should produce high VPIN (near 1.0)."""
    signals = []

    async def capture(sig):
        signals.append(sig)

    calc = VPINCalculator(bucket_size_usd=500, lookback_buckets=3, on_signal=capture)

    for _ in range(30):  # Fill multiple buckets
        trade = AggTrade(
            symbol="BTCUSDT",
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            is_buyer_maker=False,  # All buys (taker is buyer)
            trade_time=datetime.utcnow(),
        )
        await calc.on_trade(trade)

    assert len(signals) >= 3
    assert signals[-1].value > 0.9  # Should be very high


@pytest.mark.asyncio
async def test_balanced_flow_low_vpin():
    """Balanced buy/sell flow should produce low VPIN."""
    signals = []

    async def capture(sig):
        signals.append(sig)

    calc = VPINCalculator(bucket_size_usd=1000, lookback_buckets=5, on_signal=capture)

    for i in range(100):
        trade = AggTrade(
            symbol="BTCUSDT",
            price=Decimal("50000"),
            quantity=Decimal("0.002"),
            is_buyer_maker=(i % 2 == 0),  # Alternating buy/sell
            trade_time=datetime.utcnow(),
        )
        await calc.on_trade(trade)

    assert len(signals) >= 5
    assert signals[-1].value < 0.3


@pytest.mark.asyncio
async def test_no_signal_before_bucket_fills():
    """No signal emitted until a full bucket is accumulated."""
    signals = []

    async def capture(sig):
        signals.append(sig)

    calc = VPINCalculator(bucket_size_usd=10000, lookback_buckets=5, on_signal=capture)

    # Feed just 5 small trades (not enough to fill bucket)
    for _ in range(5):
        trade = _make_trade(price=Decimal("50000"), quantity=Decimal("0.001"))
        await calc.on_trade(trade)

    # 5 * 50000 * 0.001 = 250 USD, much less than 10000
    assert len(signals) == 0


@pytest.mark.asyncio
async def test_vpin_value_in_range():
    """VPIN value must always be in [0, 1]."""
    signals = []

    async def capture(sig):
        signals.append(sig)

    calc = VPINCalculator(bucket_size_usd=100, lookback_buckets=10, on_signal=capture)

    # Feed 50 trades of mixed directions
    for i in range(50):
        trade = AggTrade(
            symbol="BTCUSDT",
            price=Decimal("50000"),
            quantity=Decimal("0.002"),
            is_buyer_maker=(i % 3 != 0),  # Mostly sells
            trade_time=datetime.utcnow(),
        )
        await calc.on_trade(trade)

    for sig in signals:
        assert 0.0 <= sig.value <= 1.0, f"VPIN out of range: {sig.value}"


@pytest.mark.asyncio
async def test_all_sells_high_vpin():
    """All sell-side trades (is_buyer_maker=True) should also produce high VPIN."""
    signals = []

    async def capture(sig):
        signals.append(sig)

    calc = VPINCalculator(bucket_size_usd=500, lookback_buckets=3, on_signal=capture)

    for _ in range(30):
        trade = AggTrade(
            symbol="BTCUSDT",
            price=Decimal("50000"),
            quantity=Decimal("0.01"),
            is_buyer_maker=True,  # All sells (taker is seller)
            trade_time=datetime.utcnow(),
        )
        await calc.on_trade(trade)

    assert len(signals) >= 3
    assert signals[-1].value > 0.9


@pytest.mark.asyncio
async def test_multiple_buckets_accumulate():
    """Filling many buckets should keep rolling VPIN stable for balanced flow."""
    signals = []

    async def capture(sig):
        signals.append(sig)

    calc = VPINCalculator(bucket_size_usd=200, lookback_buckets=10, on_signal=capture)

    # Feed 200 trades of $10 each, perfectly balanced
    for i in range(200):
        trade = AggTrade(
            symbol="BTCUSDT",
            price=Decimal("10000"),
            quantity=Decimal("0.001"),
            is_buyer_maker=(i % 2 == 0),
            trade_time=datetime.utcnow(),
        )
        await calc.on_trade(trade)

    # Should have many signals
    assert len(signals) >= 10

    # Rolling VPIN should be low (balanced flow)
    final_vpin = signals[-1].value
    assert final_vpin < 0.3, f"Expected low VPIN for balanced flow, got {final_vpin}"
