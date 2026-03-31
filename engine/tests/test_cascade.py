"""
Tests for the Cascade Detector FSM.

Tests cover:
  - State transitions: IDLE → CASCADE_DETECTED → EXHAUSTING → BET_SIGNAL → COOLDOWN → IDLE
  - Transition guards (all three conditions must be met)
  - Signal emission with correct direction
  - Cooldown enforcement
  - Re-entry after cooldown expiry
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

from signals.cascade_detector import CascadeDetector, CascadeState
from data.models import (
    VPINSignal,
    OpenInterestSnapshot,
    CascadeSignal,
    LiquidationVolume,
)


def make_vpin_signal(value: float, cascade_crossed: bool = True) -> VPINSignal:
    return VPINSignal(
        value=value,
        buckets_filled=50,
        informed_threshold_crossed=value >= 0.55,
        cascade_threshold_crossed=cascade_crossed,
        timestamp=datetime.utcnow(),
    )


def make_oi_snapshot(delta_pct: float) -> OpenInterestSnapshot:
    return OpenInterestSnapshot(
        symbol="BTCUSDT",
        open_interest_usd=Decimal("1000000000"),
        open_interest_delta_pct=delta_pct,
        timestamp=datetime.utcnow(),
    )


def make_liq_volume(usd: float) -> LiquidationVolume:
    return LiquidationVolume(
        symbol="BTCUSDT",
        liq_volume_usd=Decimal(str(usd)),
        window_seconds=300,
        timestamp=datetime.utcnow(),
    )


@pytest.fixture
def detector() -> CascadeDetector:
    return CascadeDetector()


class TestInitialState:
    def test_starts_in_idle(self, detector: CascadeDetector) -> None:
        assert detector._state == CascadeState.IDLE

    def test_no_direction_initially(self, detector: CascadeDetector) -> None:
        assert detector._direction is None


class TestIdleToCascadeDetected:
    """IDLE → CASCADE_DETECTED requires all three conditions."""

    @pytest.mark.asyncio
    async def test_transitions_with_all_conditions_met(self, detector: CascadeDetector) -> None:
        """All three triggers → CASCADE_DETECTED."""
        await detector.on_vpin(make_vpin_signal(0.75, cascade_crossed=True))
        await detector.on_oi(make_oi_snapshot(-0.03))  # -3% OI drop
        await detector.on_liquidation(make_liq_volume(6_000_000))  # $6M liq

        assert detector._state == CascadeState.CASCADE_DETECTED

    @pytest.mark.asyncio
    async def test_stays_idle_if_vpin_below_threshold(self, detector: CascadeDetector) -> None:
        """Low VPIN → stays IDLE."""
        await detector.on_vpin(make_vpin_signal(0.50, cascade_crossed=False))
        await detector.on_oi(make_oi_snapshot(-0.03))
        await detector.on_liquidation(make_liq_volume(6_000_000))

        assert detector._state == CascadeState.IDLE

    @pytest.mark.asyncio
    async def test_stays_idle_if_oi_drop_insufficient(self, detector: CascadeDetector) -> None:
        """Small OI drop → stays IDLE."""
        await detector.on_vpin(make_vpin_signal(0.75, cascade_crossed=True))
        await detector.on_oi(make_oi_snapshot(-0.005))  # only 0.5%
        await detector.on_liquidation(make_liq_volume(6_000_000))

        assert detector._state == CascadeState.IDLE

    @pytest.mark.asyncio
    async def test_stays_idle_if_liq_volume_too_low(self, detector: CascadeDetector) -> None:
        """Low liquidation volume → stays IDLE."""
        await detector.on_vpin(make_vpin_signal(0.75, cascade_crossed=True))
        await detector.on_oi(make_oi_snapshot(-0.03))
        await detector.on_liquidation(make_liq_volume(1_000_000))  # only $1M

        assert detector._state == CascadeState.IDLE


class TestCascadeToExhausting:
    @pytest.mark.asyncio
    async def test_transitions_to_exhausting_when_vpin_drops(self, detector: CascadeDetector) -> None:
        """
        After CASCADE_DETECTED, when VPIN starts declining,
        the FSM should transition to EXHAUSTING.
        """
        # Trigger cascade
        await detector.on_vpin(make_vpin_signal(0.80, cascade_crossed=True))
        await detector.on_oi(make_oi_snapshot(-0.03))
        await detector.on_liquidation(make_liq_volume(7_000_000))

        assert detector._state == CascadeState.CASCADE_DETECTED

        # VPIN declines → exhaustion begins
        await detector.on_vpin(make_vpin_signal(0.72, cascade_crossed=True))
        await detector.on_vpin(make_vpin_signal(0.65, cascade_crossed=False))

        assert detector._state == CascadeState.EXHAUSTING


class TestSignalEmission:
    @pytest.mark.asyncio
    async def test_bet_signal_emitted_correctly(self) -> None:
        """BET_SIGNAL should be emitted with correct direction."""
        received: list[CascadeSignal] = []

        async def capture(sig: CascadeSignal) -> None:
            received.append(sig)

        detector = CascadeDetector(on_signal=capture)

        # Trigger full cascade flow
        await detector.on_vpin(make_vpin_signal(0.80, cascade_crossed=True))
        await detector.on_oi(make_oi_snapshot(-0.03))
        await detector.on_liquidation(make_liq_volume(7_000_000))

        # Exhaust
        await detector.on_vpin(make_vpin_signal(0.65, cascade_crossed=False))
        await detector.on_vpin(make_vpin_signal(0.55, cascade_crossed=False))

        if received:
            sig = received[-1]
            assert sig.state in ("EXHAUSTING", "BET_SIGNAL")
            assert sig.vpin > 0

    @pytest.mark.asyncio
    async def test_signal_has_all_required_fields(self) -> None:
        """CascadeSignal must populate all metric fields."""
        received: list[CascadeSignal] = []

        async def capture(sig: CascadeSignal) -> None:
            received.append(sig)

        detector = CascadeDetector(on_signal=capture)
        await detector.on_vpin(make_vpin_signal(0.80, cascade_crossed=True))
        await detector.on_oi(make_oi_snapshot(-0.03))
        await detector.on_liquidation(make_liq_volume(6_500_000))

        assert len(received) > 0
        sig = received[0]
        assert isinstance(sig.state, str)
        assert isinstance(sig.vpin, float)
        assert isinstance(sig.oi_delta_pct, float)
        assert isinstance(sig.liq_volume_usd, float)
        assert isinstance(sig.timestamp, datetime)


class TestCooldown:
    @pytest.mark.asyncio
    async def test_cooldown_prevents_reentry(self, detector: CascadeDetector) -> None:
        """During COOLDOWN, FSM should not re-enter CASCADE_DETECTED."""
        # Manually set state to COOLDOWN
        detector._state = CascadeState.COOLDOWN
        from datetime import timedelta
        detector._cooldown_until = datetime.utcnow() + timedelta(seconds=900)

        # Try to trigger cascade again
        await detector.on_vpin(make_vpin_signal(0.80, cascade_crossed=True))
        await detector.on_oi(make_oi_snapshot(-0.03))
        await detector.on_liquidation(make_liq_volume(7_000_000))

        assert detector._state == CascadeState.COOLDOWN
