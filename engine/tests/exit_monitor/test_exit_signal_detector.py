"""Unit tests for ExitSignalDetector and ShadowTrigger domain layer.

Pure logic tests — no mocks, no IO. Pinning the contract for:
  - p_against computation for UP vs DN side
  - threshold crossing detection
  - first-cross-only guarantee (no re-fire on same threshold)
  - all thresholds evaluated per tick
  - returns empty list when no threshold fires
  - ShadowTrigger value object fields
"""
from __future__ import annotations

import pytest

from exit_monitor.domain.exit_signal_detector import (
    DEFAULT_THRESHOLDS,
    ExitSignalDetector,
    _compute_p_against,
)
from exit_monitor.domain.shadow_trigger import ShadowTrigger


# ── _compute_p_against ───────────────────────────────────────────────────────


class TestComputePAgainst:
    """p_against computation for both sides."""

    def test_up_side_p_against_is_complement(self):
        """For UP trade, p_against = 1 - P(UP)."""
        assert abs(_compute_p_against(0.75, "UP") - 0.25) < 1e-9

    def test_dn_side_p_against_is_prob_up(self):
        """For DN trade, p_against = P(UP) directly."""
        assert abs(_compute_p_against(0.75, "DN") - 0.75) < 1e-9

    def test_edge_prob_zero(self):
        """P(UP)=0 → UP side p_against=1.0, DN side p_against=0.0."""
        assert abs(_compute_p_against(0.0, "UP") - 1.0) < 1e-9
        assert abs(_compute_p_against(0.0, "DN") - 0.0) < 1e-9

    def test_edge_prob_one(self):
        """P(UP)=1 → UP side p_against=0.0, DN side p_against=1.0."""
        assert abs(_compute_p_against(1.0, "UP") - 0.0) < 1e-9
        assert abs(_compute_p_against(1.0, "DN") - 1.0) < 1e-9


# ── ExitSignalDetector ──────────────────────────────────────────────────────


_BASE = dict(
    decision_id=42,
    asset="BTC",
    window_ts=1_777_200_000,
    strategy_id="tickformer_v18_t180",
    tickformer_model="v18",
    eval_offset=150,
)


class TestNoTriggerBelowThreshold:
    """No trigger when p_against < lowest threshold."""

    def test_no_trigger_when_prob_high_for_up(self):
        """UP trade at P(UP)=0.80 → p_against=0.20, below all thresholds."""
        det = ExitSignalDetector()
        result = det.should_shadow_exit(
            side="UP", prob_tickformer=0.80, **_BASE
        )
        assert result == []

    def test_no_trigger_when_prob_low_for_dn(self):
        """DN trade at P(UP)=0.20 → p_against=0.20, below all thresholds."""
        det = ExitSignalDetector()
        result = det.should_shadow_exit(
            side="DN", prob_tickformer=0.20, **_BASE
        )
        assert result == []


class TestThresholdCrossing:
    """Threshold crossing detection."""

    def test_fires_at_p50(self):
        """p_against=0.51 crosses 0.50 threshold."""
        det = ExitSignalDetector()
        triggers = det.should_shadow_exit(
            side="UP", prob_tickformer=0.49, **_BASE  # p_against = 1-0.49 = 0.51
        )
        thresholds_fired = {t.threshold for t in triggers}
        assert 0.50 in thresholds_fired

    def test_fires_multiple_thresholds_when_p_against_is_high(self):
        """p_against=0.70 crosses 0.50, 0.55, 0.60, 0.65."""
        det = ExitSignalDetector()
        triggers = det.should_shadow_exit(
            side="DN", prob_tickformer=0.70, **_BASE  # p_against=0.70
        )
        thresholds_fired = {t.threshold for t in triggers}
        assert thresholds_fired == {0.50, 0.55, 0.60, 0.65}

    def test_fires_only_crossed_thresholds(self):
        """p_against=0.57 crosses 0.50 and 0.55 only."""
        det = ExitSignalDetector()
        triggers = det.should_shadow_exit(
            side="DN", prob_tickformer=0.57, **_BASE
        )
        thresholds_fired = {t.threshold for t in triggers}
        assert thresholds_fired == {0.50, 0.55}
        assert 0.60 not in thresholds_fired
        assert 0.65 not in thresholds_fired


class TestFirstCrossOnly:
    """First-cross-only: same threshold doesn't re-fire within same trade."""

    def test_no_refire_on_second_tick(self):
        """After 0.50 fires once, it never fires again for this detector."""
        det = ExitSignalDetector()
        # First tick: fires
        first = det.should_shadow_exit(
            side="DN", prob_tickformer=0.55, **_BASE
        )
        assert any(t.threshold == 0.50 for t in first)
        # Second tick: same signal — 0.50 should NOT fire again
        second = det.should_shadow_exit(
            side="DN", prob_tickformer=0.55, **_BASE
        )
        assert not any(t.threshold == 0.50 for t in second)

    def test_higher_threshold_can_fire_later(self):
        """After 0.50 fires, 0.65 can still fire on next tick."""
        det = ExitSignalDetector()
        det.should_shadow_exit(side="DN", prob_tickformer=0.51, **_BASE)  # fires 0.50
        # Later tick: p_against rises to 0.70 → 0.55, 0.60, 0.65 fire
        triggers = det.should_shadow_exit(
            side="DN", prob_tickformer=0.70, **_BASE
        )
        thresholds_fired = {t.threshold for t in triggers}
        # 0.50 already fired, so only 0.55, 0.60, 0.65 should fire
        assert 0.50 not in thresholds_fired
        assert {0.55, 0.60, 0.65}.issubset(thresholds_fired)

    def test_reset_clears_fired_set(self):
        """reset() lets all thresholds fire again (for position recycling)."""
        det = ExitSignalDetector()
        det.should_shadow_exit(side="DN", prob_tickformer=0.70, **_BASE)
        det.reset()
        triggers = det.should_shadow_exit(
            side="DN", prob_tickformer=0.70, **_BASE
        )
        thresholds_fired = {t.threshold for t in triggers}
        assert {0.50, 0.55, 0.60, 0.65}.issubset(thresholds_fired)


class TestShadowTriggerFields:
    """ShadowTrigger value object contains correct fields."""

    def test_trigger_fields_populated(self):
        """Check all expected fields on a returned ShadowTrigger."""
        det = ExitSignalDetector()
        triggers = det.should_shadow_exit(
            side="UP",
            prob_tickformer=0.40,  # p_against = 0.60
            eval_offset=120,
            entry_p=0.88,
            entry_eval_offset=210,
            **{k: v for k, v in _BASE.items() if k != "eval_offset"},
        )
        t55 = next(t for t in triggers if t.threshold == 0.55)
        assert t55.decision_id == 42
        assert t55.asset == "BTC"
        assert t55.window_ts == 1_777_200_000
        assert t55.strategy_id == "tickformer_v18_t180"
        assert t55.side == "UP"
        assert t55.trigger_eval_offset == 120
        assert abs(t55.p_against - 0.60) < 1e-9
        assert abs(t55.p_for - 0.40) < 1e-9
        assert t55.tickformer_model == "v18"
        assert t55.entry_p == 0.88
        assert t55.entry_eval_offset == 210

    def test_trigger_is_frozen(self):
        """ShadowTrigger is immutable (frozen dataclass)."""
        trig = ShadowTrigger(
            decision_id=1,
            asset="ETH",
            window_ts=100,
            strategy_id="s",
            side="DN",
            trigger_eval_offset=90,
            threshold=0.55,
            p_against=0.60,
            p_for=0.40,
            tickformer_model="v18",
        )
        with pytest.raises(Exception):
            trig.threshold = 0.65  # type: ignore[misc]


class TestCustomThresholds:
    """Custom threshold tuples are respected."""

    def test_single_threshold(self):
        """Only the one threshold supplied is evaluated."""
        det = ExitSignalDetector()
        triggers = det.should_shadow_exit(
            side="DN",
            prob_tickformer=0.70,
            thresholds=(0.65,),
            **_BASE,
        )
        assert len(triggers) == 1
        assert triggers[0].threshold == 0.65

    def test_empty_thresholds(self):
        """No thresholds → empty list always."""
        det = ExitSignalDetector()
        triggers = det.should_shadow_exit(
            side="DN",
            prob_tickformer=0.90,
            thresholds=(),
            **_BASE,
        )
        assert triggers == []
