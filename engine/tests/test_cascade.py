"""
Tests for CascadeDetector FSM.

Tests state transitions, direction assignment, and cooldown behaviour.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

from data.models import CascadeSignal
from signals.cascade_detector import (
    CascadeDetector,
    COOLDOWN_SECONDS,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _make_cascade_inputs(
    vpin: float = 0.75,
    oi_delta_pct: float = -0.03,
    liq_volume_5m: float = 6_000_000.0,
    btc_price: float = 60_000.0,
    btc_price_5m_ago: float = 63_000.0,  # price fell → cascade "down"
) -> dict:
    return dict(
        vpin=vpin,
        oi_delta_pct=oi_delta_pct,
        liq_volume_5m=liq_volume_5m,
        btc_price=btc_price,
        btc_price_5m_ago=btc_price_5m_ago,
    )


# ─── Basic State Tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_initial_state_is_idle():
    """Detector starts in IDLE state."""
    detector = CascadeDetector()
    assert detector.state == CascadeDetector.IDLE
    assert detector.direction is None


@pytest.mark.asyncio
async def test_idle_to_cascade_detected():
    """IDLE → CASCADE_DETECTED when VPIN high + OI drop + liq volume."""
    signals: list[CascadeSignal] = []

    async def capture(sig: CascadeSignal):
        signals.append(sig)

    detector = CascadeDetector(on_signal=capture)

    await detector.update(**_make_cascade_inputs())

    assert detector.state == CascadeDetector.CASCADE_DETECTED
    assert len(signals) == 1
    assert signals[0].state == CascadeDetector.CASCADE_DETECTED


@pytest.mark.asyncio
async def test_idle_no_transition_without_conditions():
    """IDLE stays IDLE if conditions don't meet threshold."""
    signals: list[CascadeSignal] = []

    async def capture(sig: CascadeSignal):
        signals.append(sig)

    detector = CascadeDetector(on_signal=capture)

    # Low VPIN — shouldn't trigger
    await detector.update(
        vpin=0.5,
        oi_delta_pct=-0.03,
        liq_volume_5m=6_000_000.0,
        btc_price=60_000.0,
        btc_price_5m_ago=63_000.0,
    )

    assert detector.state == CascadeDetector.IDLE
    assert len(signals) == 0


@pytest.mark.asyncio
async def test_idle_no_transition_low_liq():
    """IDLE stays IDLE if liquidation volume is too low."""
    detector = CascadeDetector()

    await detector.update(
        vpin=0.75,
        oi_delta_pct=-0.03,
        liq_volume_5m=1_000_000.0,  # Below threshold
        btc_price=60_000.0,
        btc_price_5m_ago=63_000.0,
    )

    assert detector.state == CascadeDetector.IDLE


@pytest.mark.asyncio
async def test_idle_no_transition_low_oi_delta():
    """IDLE stays IDLE if OI delta is below threshold."""
    detector = CascadeDetector()

    await detector.update(
        vpin=0.75,
        oi_delta_pct=-0.005,  # Below 2% threshold
        liq_volume_5m=6_000_000.0,
        btc_price=60_000.0,
        btc_price_5m_ago=63_000.0,
    )

    assert detector.state == CascadeDetector.IDLE


# ─── Full FSM Cycle ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_cycle_idle_to_cooldown():
    """Test full FSM cycle: IDLE → CASCADE_DETECTED → EXHAUSTING → BET_SIGNAL → COOLDOWN."""
    signals: list[CascadeSignal] = []

    async def capture(sig: CascadeSignal):
        signals.append(sig)

    detector = CascadeDetector(on_signal=capture)

    # Step 1: IDLE → CASCADE_DETECTED
    await detector.update(**_make_cascade_inputs(
        vpin=0.75,
        liq_volume_5m=6_000_000.0,
    ))
    assert detector.state == CascadeDetector.CASCADE_DETECTED

    # Step 2: CASCADE_DETECTED → EXHAUSTING (liq declining)
    await detector.update(**_make_cascade_inputs(
        vpin=0.72,
        liq_volume_5m=4_500_000.0,  # < 85% of 6M → declining
    ))
    assert detector.state == CascadeDetector.EXHAUSTING

    # Step 3: EXHAUSTING → BET_SIGNAL → COOLDOWN
    await detector.update(**_make_cascade_inputs(
        vpin=0.45,                   # Below exhaustion threshold 0.55
        liq_volume_5m=2_000_000.0,  # Below 2.5M threshold
    ))
    # After BET_SIGNAL, immediately transitions to COOLDOWN
    assert detector.state == CascadeDetector.COOLDOWN

    # Verify signals emitted
    states_seen = [s.state for s in signals]
    assert CascadeDetector.CASCADE_DETECTED in states_seen
    assert CascadeDetector.EXHAUSTING in states_seen
    assert CascadeDetector.BET_SIGNAL in states_seen


@pytest.mark.asyncio
async def test_direction_down_for_falling_price():
    """Cascade direction should be 'down' when BTC price has fallen."""
    signals: list[CascadeSignal] = []

    async def capture(sig: CascadeSignal):
        signals.append(sig)

    detector = CascadeDetector(on_signal=capture)

    # Price fell: 63000 → 60000
    await detector.update(**_make_cascade_inputs(
        btc_price=60_000.0,
        btc_price_5m_ago=63_000.0,
    ))

    assert detector.state == CascadeDetector.CASCADE_DETECTED
    assert detector.direction == "down"
    assert signals[0].direction == "down"


@pytest.mark.asyncio
async def test_direction_up_for_rising_price():
    """Cascade direction should be 'up' when BTC price has risen."""
    signals: list[CascadeSignal] = []

    async def capture(sig: CascadeSignal):
        signals.append(sig)

    detector = CascadeDetector(on_signal=capture)

    # Price rose: 60000 → 63000
    await detector.update(**_make_cascade_inputs(
        btc_price=63_000.0,
        btc_price_5m_ago=60_000.0,
    ))

    assert detector.state == CascadeDetector.CASCADE_DETECTED
    assert detector.direction == "up"


@pytest.mark.asyncio
async def test_cascade_to_exhausting_on_vpin_drop():
    """CASCADE_DETECTED → EXHAUSTING when VPIN drops below 0.70."""
    detector = CascadeDetector()

    # Enter CASCADE_DETECTED
    await detector.update(**_make_cascade_inputs(vpin=0.75, liq_volume_5m=6_000_000.0))
    assert detector.state == CascadeDetector.CASCADE_DETECTED

    # VPIN drops below 0.70 threshold
    await detector.update(**_make_cascade_inputs(vpin=0.65, liq_volume_5m=5_500_000.0))
    assert detector.state == CascadeDetector.EXHAUSTING


# ─── Cooldown Tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_prevents_reentry():
    """Once in COOLDOWN, should not re-trigger CASCADE_DETECTED."""
    detector = CascadeDetector()

    # Trigger full cascade → ends up in COOLDOWN
    await detector.update(**_make_cascade_inputs(vpin=0.75, liq_volume_5m=6_000_000.0))
    await detector.update(**_make_cascade_inputs(vpin=0.72, liq_volume_5m=4_500_000.0))
    await detector.update(**_make_cascade_inputs(vpin=0.45, liq_volume_5m=2_000_000.0))
    assert detector.state == CascadeDetector.COOLDOWN

    # Even with full cascade conditions, should stay in COOLDOWN
    await detector.update(**_make_cascade_inputs(vpin=0.80, liq_volume_5m=8_000_000.0))
    assert detector.state == CascadeDetector.COOLDOWN


@pytest.mark.asyncio
async def test_cooldown_expires_to_idle():
    """After COOLDOWN_SECONDS, FSM returns to IDLE."""
    detector = CascadeDetector()

    # Force into COOLDOWN quickly
    await detector.update(**_make_cascade_inputs(vpin=0.75, liq_volume_5m=6_000_000.0))
    await detector.update(**_make_cascade_inputs(vpin=0.72, liq_volume_5m=4_500_000.0))
    await detector.update(**_make_cascade_inputs(vpin=0.45, liq_volume_5m=2_000_000.0))
    assert detector.state == CascadeDetector.COOLDOWN

    # Simulate cooldown expiry by manipulating the internal timestamp
    detector._cooldown_start = time.monotonic() - COOLDOWN_SECONDS - 1

    # Next update should transition back to IDLE
    await detector.update(**_make_cascade_inputs(vpin=0.3, liq_volume_5m=500_000.0))
    assert detector.state == CascadeDetector.IDLE
    assert detector.direction is None


@pytest.mark.asyncio
async def test_signal_emitted_on_bet_signal_state():
    """BET_SIGNAL state must emit a CascadeSignal with correct fields."""
    signals: list[CascadeSignal] = []

    async def capture(sig: CascadeSignal):
        signals.append(sig)

    detector = CascadeDetector(on_signal=capture)

    # Drive to BET_SIGNAL
    await detector.update(**_make_cascade_inputs(vpin=0.75, liq_volume_5m=6_000_000.0))
    await detector.update(**_make_cascade_inputs(vpin=0.72, liq_volume_5m=4_500_000.0))
    await detector.update(**_make_cascade_inputs(vpin=0.45, liq_volume_5m=2_000_000.0))

    bet_signals = [s for s in signals if s.state == CascadeDetector.BET_SIGNAL]
    assert len(bet_signals) == 1

    sig = bet_signals[0]
    assert sig.direction in ("up", "down")
    assert 0 <= sig.vpin <= 1
    assert sig.liq_volume_usd >= 0
    assert sig.timestamp is not None
