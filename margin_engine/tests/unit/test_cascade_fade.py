"""
Unit tests for Cascade Fade Strategy (ME-STRAT-05).

Tests cover:
- Cascade state machine (IDLE, CASCADE, BET, COOLDOWN)
- Direction detection (LONG vs SHORT liquidations)
- Fade direction (opposite of cascade)
- Entry quality (PREMIUM, STANDARD, LATE)
- Size multiplier based on quality
- Cooldown logic
- Weak cascade → no trade
- Integration with position opener
"""

import pytest
from datetime import datetime, timedelta
from typing import Optional

from margin_engine.application.services.strategy import TradeDecision
from margin_engine.domain.value_objects import (
    V4Snapshot,
    TimescalePayload,
    Quantiles,
    Consensus,
    MacroBias,
    Cascade,
)
from margin_engine.application.services.cascade_detector import (
    analyze_cascade,
    CascadeState,
    CascadeInfo,
)
from margin_engine.application.services.cascade_fade import (
    CascadeFadeStrategy,
    CascadeFadeConfig,
)


# ─────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────


def create_v4_snapshot(
    regime: str = "TRENDING_UP",
    probability_up: Optional[float] = 0.65,
    expected_move_bps: Optional[float] = 50.0,
    status: str = "ok",
    timescale: str = "15m",
    cascade_strength: Optional[float] = None,
    cascade_tau1: Optional[float] = None,
    cascade_tau2: Optional[float] = None,
    cascade_exhaustion_t: Optional[float] = None,
    cascade_signal: Optional[str] = None,
    composite_v3: Optional[float] = None,
) -> V4Snapshot:
    """Create a mock V4 snapshot for testing."""
    cascade_data = Cascade(
        strength=cascade_strength,
        tau1=cascade_tau1,
        tau2=cascade_tau2,
        exhaustion_t=cascade_exhaustion_t,
        signal=cascade_signal,
    )

    return V4Snapshot(
        asset="BTC",
        ts=1713000000.0,
        last_price=65000.0,
        timescales={
            timescale: TimescalePayload(
                timescale=timescale,
                status=status,
                probability_up=probability_up,
                regime=regime,
                expected_move_bps=expected_move_bps,
                quantiles_at_close=Quantiles(p10=64500.0, p90=65500.0),
                cascade=cascade_data,
                composite_v3=composite_v3,
            )
        },
        consensus=Consensus(safe_to_trade=True),
        macro=MacroBias(status="ok"),
    )


# ─────────────────────────────────────────────────────────────────────────
# Cascade Detector Tests
# ─────────────────────────────────────────────────────────────────────────


class TestCascadeDetector:
    """Tests for cascade state machine."""

    def test_no_cascade_data(self):
        """No cascade data → IDLE state."""
        v4 = create_v4_snapshot(cascade_strength=None)
        result = analyze_cascade(v4)

        assert result.state == CascadeState.IDLE
        assert result.direction is None
        assert result.strength == 0.0
        assert result.time_to_exhaustion_s == 0.0
        assert result.entry_quality == "NONE"
        assert result.is_safe_to_fade is False

    def test_weak_cascade(self):
        """Weak cascade (0 < strength < 0.3) → IDLE state, LATE quality."""
        v4 = create_v4_snapshot(
            cascade_strength=0.2,
            composite_v3=0.5,
        )
        result = analyze_cascade(v4)

        assert result.state == CascadeState.IDLE
        assert result.strength == 0.2
        assert result.entry_quality == "LATE"
        assert result.is_safe_to_fade is False

    def test_cascade_id_to_cascade_state(self):
        """Cascade strength 0.5-0.7 → BET state."""
        v4 = create_v4_snapshot(
            cascade_strength=0.6,
            cascade_tau1=300.0,
            cascade_tau2=600.0,
            cascade_exhaustion_t=900.0,
            composite_v3=0.5,
        )
        result = analyze_cascade(v4)

        assert result.state == CascadeState.BET
        assert result.strength == 0.6
        assert result.entry_quality == "STANDARD"
        assert result.is_safe_to_fade is True
        assert result.time_to_exhaustion_s == 900.0

    def test_strong_cascade(self):
        """Strong cascade (strength >= 0.7) → CASCADE state."""
        v4 = create_v4_snapshot(
            cascade_strength=0.85,
            cascade_tau1=300.0,
            cascade_tau2=600.0,
            cascade_exhaustion_t=900.0,
            composite_v3=0.5,
        )
        result = analyze_cascade(v4)

        assert result.state == CascadeState.CASCADE
        assert result.strength == 0.85
        assert result.entry_quality == "PREMIUM"
        assert result.is_safe_to_fade is True

    def test_cascade_direction_long_liquidations(self):
        """Positive composite → SHORT liquidations → direction=SHORT."""
        v4 = create_v4_snapshot(
            cascade_strength=0.75,
            composite_v3=0.5,  # Positive = price up = LONGs getting liquidated
        )
        result = analyze_cascade(v4)

        assert result.direction == "SHORT"
        assert result.is_safe_to_fade is True

    def test_cascade_direction_short_liquidations(self):
        """Negative composite → LONG liquidations → direction=LONG."""
        v4 = create_v4_snapshot(
            cascade_strength=0.75,
            composite_v3=-0.5,  # Negative = price down = SHORTs getting liquidated
        )
        result = analyze_cascade(v4)

        assert result.direction == "LONG"
        assert result.is_safe_to_fade is True

    def test_cascade_no_direction_when_composite_zero(self):
        """Zero composite → no direction determined."""
        v4 = create_v4_snapshot(
            cascade_strength=0.75,
            composite_v3=0.0,
        )
        result = analyze_cascade(v4)

        assert result.direction is None
        assert result.is_safe_to_fade is False


# ─────────────────────────────────────────────────────────────────────────
# Cascade Fade Strategy Tests
# ─────────────────────────────────────────────────────────────────────────


class TestCascadeFadeStrategy:
    """Tests for cascade fade strategy."""

    def test_cascade_not_active(self):
        """No active cascade → no trade."""
        v4 = create_v4_snapshot(cascade_strength=None)
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "CASCADE_NOT_ACTIVE"

    def test_weak_cascade_not_safe(self):
        """Weak cascade (strength < 0.5) → no trade."""
        v4 = create_v4_snapshot(
            cascade_strength=0.4,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "CASCADE_NOT_ACTIVE"

    def test_fade_long_liquidations(self):
        """LONG liquidations (negative composite) → bet SHORT."""
        v4 = create_v4_snapshot(
            cascade_strength=0.75,
            composite_v3=-0.5,  # Negative = SHORTs getting liquidated = LONG liquidations
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "SHORT"  # Fade: bet against LONG liquidations
        assert decision.is_trade is True
        assert "CASCADE_FADE_LONG" in decision.reason

    def test_fade_short_liquidations(self):
        """SHORT liquidations (positive composite) → bet LONG."""
        v4 = create_v4_snapshot(
            cascade_strength=0.75,
            composite_v3=0.5,  # Positive = LONGs getting liquidated = SHORT liquidations
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "LONG"  # Fade: bet against SHORT liquidations
        assert decision.is_trade is True
        assert "CASCADE_FADE_SHORT" in decision.reason

    def test_premium_entry_size(self):
        """PREMIUM entry (strength >= 0.7) → 0.6x size."""
        v4 = create_v4_snapshot(
            cascade_strength=0.85,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "LONG"
        assert decision.size_mult == 0.6  # 0.5 * 1.2
        assert decision.stop_loss_bps == 300
        assert decision.take_profit_bps == 100
        assert decision.hold_minutes == 10

    def test_standard_entry_size(self):
        """STANDARD entry (0.5 <= strength < 0.7) → 0.5x size."""
        v4 = create_v4_snapshot(
            cascade_strength=0.6,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "LONG"
        assert decision.size_mult == 0.5
        assert decision.stop_loss_bps == 300
        assert decision.take_profit_bps == 100

    def test_late_entry_no_trade(self):
        """LATE entry (strength < 0.5) → no trade."""
        v4 = create_v4_snapshot(
            cascade_strength=0.45,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "CASCADE_NOT_ACTIVE"

    def test_custom_config(self):
        """Cascade fade with custom configuration."""
        config = CascadeFadeConfig(
            min_cascade_strength=0.6,
            size_mult=0.7,
            stop_loss_bps=400,
            take_profit_bps=150,
            hold_minutes=15,
            cooldown_seconds=1800,
        )
        v4 = create_v4_snapshot(
            cascade_strength=0.85,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy(config=config)
        decision = strategy.decide(v4)

        assert decision.direction == "LONG"
        assert decision.size_mult == 0.84  # 0.7 * 1.2 for PREMIUM
        assert decision.stop_loss_bps == 400
        assert decision.take_profit_bps == 150
        assert decision.hold_minutes == 15


# ─────────────────────────────────────────────────────────────────────────
# Cooldown Tests
# ─────────────────────────────────────────────────────────────────────────


class TestCascadeFadeCooldown:
    """Tests for cascade fade cooldown logic."""

    def test_no_cooldown_initially(self):
        """No previous cascade → no cooldown."""
        v4 = create_v4_snapshot(
            cascade_strength=0.85,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is not None
        assert decision.is_trade is True

    def test_cooldown_after_cascade_end(self):
        """In cooldown after cascade end → no trade."""
        v4 = create_v4_snapshot(
            cascade_strength=0.85,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy()

        # Simulate cascade end
        strategy.on_cascade_end()

        # Should be in cooldown
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "CASCADE_COOLDOWN"

    def test_cooldown_expires(self):
        """Cooldown expires after configured time."""
        v4 = create_v4_snapshot(
            cascade_strength=0.85,
            composite_v3=0.5,
        )
        config = CascadeFadeConfig(cooldown_seconds=1)  # 1 second cooldown
        strategy = CascadeFadeStrategy(config=config)

        # Simulate cascade end
        strategy.on_cascade_end()

        # Should be in cooldown initially
        decision = strategy.decide(v4)
        assert decision.reason == "CASCADE_COOLDOWN"

        # Wait for cooldown to expire
        import time

        time.sleep(1.1)

        # Should be able to trade again
        decision = strategy.decide(v4)
        assert decision.direction is not None
        assert decision.is_trade is True


# ─────────────────────────────────────────────────────────────────────────
# Edge Cases
# ─────────────────────────────────────────────────────────────────────────


class TestCascadeFadeEdgeCases:
    """Edge case tests."""

    def test_missing_timescale(self):
        """Missing 15m timescale → no trade."""
        v4 = create_v4_snapshot(timescale="1h", cascade_strength=0.85)
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "CASCADE_NOT_ACTIVE"

    def test_missing_composite(self):
        """Missing composite_v3 → no direction → no trade."""
        v4 = create_v4_snapshot(
            cascade_strength=0.85,
            composite_v3=None,
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "CASCADE_NOT_ACTIVE"

    def test_zero_strength(self):
        """Zero cascade strength → IDLE → no trade."""
        v4 = create_v4_snapshot(
            cascade_strength=0.0,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "CASCADE_NOT_ACTIVE"

    def test_boundary_strength_0_5(self):
        """Boundary case: strength exactly 0.5 → BET state, safe to fade."""
        v4 = create_v4_snapshot(
            cascade_strength=0.5,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "LONG"
        assert decision.is_trade is True
        assert decision.size_mult == 0.5  # STANDARD entry

    def test_boundary_strength_0_7(self):
        """Boundary case: strength exactly 0.7 → CASCADE state, PREMIUM."""
        v4 = create_v4_snapshot(
            cascade_strength=0.7,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "LONG"
        assert decision.is_trade is True
        assert decision.size_mult == 0.6  # PREMIUM entry (0.5 * 1.2)


# ─────────────────────────────────────────────────────────────────────────
# Integration Tests
# ─────────────────────────────────────────────────────────────────────────


class TestCascadeFadeIntegration:
    """Integration tests with full V4 snapshot flow."""

    def test_full_cascade_fade_flow(self):
        """Full cascade fade flow from detection to trade."""
        v4 = create_v4_snapshot(
            regime="TRENDING_DOWN",
            probability_up=0.35,
            cascade_strength=0.85,
            cascade_tau1=300.0,
            cascade_tau2=600.0,
            cascade_exhaustion_t=900.0,
            cascade_signal="CASCADE",
            composite_v3=0.5,  # SHORT liquidations
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        # Should fade SHORT liquidations with LONG position
        assert decision.direction == "LONG"
        assert decision.is_trade is True
        assert decision.size_mult == 0.6  # PREMIUM
        assert decision.stop_loss_bps == 300
        assert decision.take_profit_bps == 100
        assert decision.hold_minutes == 10
        assert "CASCADE_FADE_SHORT_PREMIUM" in decision.reason
        assert decision.reward_risk_ratio == 100 / 300  # 0.33

    def test_cascade_state_machine_transitions(self):
        """Test cascade state machine transitions."""
        # IDLE state
        v4_idle = create_v4_snapshot(cascade_strength=0.2)
        result_idle = analyze_cascade(v4_idle)
        assert result_idle.state == CascadeState.IDLE

        # BET state
        v4_bet = create_v4_snapshot(cascade_strength=0.6)
        result_bet = analyze_cascade(v4_bet)
        assert result_bet.state == CascadeState.BET

        # CASCADE state
        v4_cascade = create_v4_snapshot(cascade_strength=0.85)
        result_cascade = analyze_cascade(v4_cascade)
        assert result_cascade.state == CascadeState.CASCADE

    def test_reward_risk_ratio(self):
        """Verify reward-risk ratio calculation."""
        v4 = create_v4_snapshot(
            cascade_strength=0.85,
            composite_v3=0.5,
        )
        strategy = CascadeFadeStrategy()
        decision = strategy.decide(v4)

        # TP=100 bps, SL=300 bps → RR = 100/300 = 0.33
        assert decision.reward_risk_ratio == pytest.approx(0.333, rel=0.01)
