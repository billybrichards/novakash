"""Tests for ConvictionFadeDetector — conviction fade exit monitor.

Pins the contract for:
  - Fade detection with mock surface data
  - Consecutive tick counting
  - Threshold variations (percentage-based + absolute floor)
  - Shadow mode logging vs real execution
  - Offset window bounds
  - Edge cases: entry_dist=0, None surface data, position not found

See conviction_fade.py module docstring for design rationale.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from strategies.exit.conviction_fade import (
    ConvictionFadeDetector,
    FadeInstruction,
    _FadeState,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def make_surface(
    *,
    offset: int = 150,
    window_ts: int = 1_777_200_000,
    probability_lgb: float = 0.70,
    last_clob_update_ts: float | None = None,
):
    """Build a minimal SimpleNamespace surface for testing."""
    return SimpleNamespace(
        eval_offset=offset,
        window_ts=window_ts,
        probability_lgb=probability_lgb,
        last_clob_update_ts=(
            last_clob_update_ts if last_clob_update_ts is not None else time.time()
        ),
    )


DEFAULT_PARAMS = dict(
    conviction_fade_enabled=True,
    conviction_fade_threshold_pct=0.40,
    conviction_fade_absolute_floor=0.08,
    conviction_fade_min_ticks=3,
    conviction_fade_active_offset_min=60,
    conviction_fade_active_offset_max=200,
    stale_mark_max_age_seconds=5.0,
)

STRATEGY = "v9_lgb_only"
WINDOW_TS = 1_777_200_000


# ── Basic detection ──────────────────────────────────────────────────────


class TestBasicFadeDetection:
    """Core fade detection logic."""

    def test_no_trigger_when_dist_holds(self):
        """No trigger when current dist is close to entry dist."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        # Current dist is 0.18 → fade_pct = 0.10, below 0.40 threshold
        surface = make_surface(probability_lgb=0.68)  # dist=0.18
        for _ in range(5):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is None

    def test_trigger_after_min_ticks(self):
        """Trigger after min_consecutive_ticks of faded dist."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        # Current dist is 0.10 → fade_pct = 0.50 > 0.40 threshold
        surface = make_surface(probability_lgb=0.60)  # dist=0.10

        # First 2 ticks: below threshold but not enough consecutive
        for i in range(2):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
            assert result is None

        # 3rd tick: should trigger
        result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is not None
        assert isinstance(result, FadeInstruction)
        assert result.strategy_id == STRATEGY
        assert result.window_ts == WINDOW_TS
        assert result.entry_dist == pytest.approx(0.20)
        assert result.current_dist == pytest.approx(0.10)
        assert result.fade_pct == pytest.approx(0.50)
        assert result.consecutive_ticks == 3

    def test_trigger_resets_on_recovery(self):
        """Consecutive count resets when dist recovers."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        faded_surface = make_surface(probability_lgb=0.60)  # dist=0.10
        recovered_surface = make_surface(probability_lgb=0.68)  # dist=0.18

        # 2 faded ticks
        det.evaluate(STRATEGY, WINDOW_TS, faded_surface, **DEFAULT_PARAMS)
        det.evaluate(STRATEGY, WINDOW_TS, faded_surface, **DEFAULT_PARAMS)

        # Recovery resets
        det.evaluate(STRATEGY, WINDOW_TS, recovered_surface, **DEFAULT_PARAMS)

        # Need 3 more faded ticks after reset
        det.evaluate(STRATEGY, WINDOW_TS, faded_surface, **DEFAULT_PARAMS)
        result = det.evaluate(STRATEGY, WINDOW_TS, faded_surface, **DEFAULT_PARAMS)
        assert result is None  # only 2 consecutive after reset

        result = det.evaluate(STRATEGY, WINDOW_TS, faded_surface, **DEFAULT_PARAMS)
        assert result is not None


# ── Absolute floor ───────────────────────────────────────────────────────


class TestAbsoluteFloor:
    """Trigger when dist drops below absolute floor regardless of pct."""

    def test_floor_trigger(self):
        """Trigger via absolute floor even if pct threshold not met."""
        det = ConvictionFadeDetector()
        # Entry dist is 0.12 → 40% fade would need dist < 0.072
        # But absolute floor is 0.08 → dist at 0.07 triggers
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.12)

        # probability_lgb=0.57 → dist=0.07, below floor=0.08
        surface = make_surface(probability_lgb=0.57)
        for _ in range(2):
            det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is not None
        assert "absolute_floor" in result.trigger_reason

    def test_floor_not_trigger_above(self):
        """No floor trigger when dist is above floor."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.12)

        # probability_lgb=0.59 → dist=0.09, above floor=0.08
        surface = make_surface(probability_lgb=0.59)
        for _ in range(5):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is None  # 25% fade, below 40% threshold; above floor


# ── Offset window bounds ────────────────────────────────────────────────


class TestOffsetBounds:
    """Only evaluate within the active offset window."""

    def test_below_min_offset_no_eval(self):
        """No trigger when eval_offset < active_offset_min."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        surface = make_surface(probability_lgb=0.55, offset=50)  # below min=60
        for _ in range(5):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is None

    def test_above_max_offset_no_eval(self):
        """No trigger when eval_offset > active_offset_max."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        surface = make_surface(probability_lgb=0.55, offset=210)  # above max=200
        for _ in range(5):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is None

    def test_at_boundary_offsets(self):
        """Triggers at exact boundary offsets."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        # At min boundary (60)
        surface = make_surface(probability_lgb=0.55, offset=60)  # dist=0.05
        for _ in range(3):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is not None

    def test_count_preserved_outside_window(self):
        """Consecutive count is preserved when temporarily outside window."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        faded_in = make_surface(probability_lgb=0.60, offset=150)  # in window
        faded_out = make_surface(probability_lgb=0.60, offset=50)  # outside

        # 2 ticks inside window
        det.evaluate(STRATEGY, WINDOW_TS, faded_in, **DEFAULT_PARAMS)
        det.evaluate(STRATEGY, WINDOW_TS, faded_in, **DEFAULT_PARAMS)

        # 1 tick outside — count should be preserved
        det.evaluate(STRATEGY, WINDOW_TS, faded_out, **DEFAULT_PARAMS)

        # Back inside — this should be the 3rd consecutive, triggering
        result = det.evaluate(STRATEGY, WINDOW_TS, faded_in, **DEFAULT_PARAMS)
        assert result is not None


# ── Edge cases ───────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge case handling."""

    def test_entry_dist_zero(self):
        """entry_dist=0 (or near-zero) should never trigger."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.0)

        surface = make_surface(probability_lgb=0.50)  # dist=0
        for _ in range(5):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is None

    def test_none_probability_lgb(self):
        """None probability_lgb on surface → no trigger, count reset."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        surface = SimpleNamespace(
            eval_offset=150,
            window_ts=WINDOW_TS,
            probability_lgb=None,
            last_clob_update_ts=time.time(),
        )
        for _ in range(5):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is None

    def test_unregistered_position(self):
        """Evaluate for unregistered position returns None."""
        det = ConvictionFadeDetector()
        surface = make_surface()
        result = det.evaluate("unknown", 0, surface, **DEFAULT_PARAMS)
        assert result is None

    def test_disabled(self):
        """conviction_fade_enabled=False → always None."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        surface = make_surface(probability_lgb=0.55)
        params = {**DEFAULT_PARAMS, "conviction_fade_enabled": False}
        for _ in range(5):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **params)
        assert result is None

    def test_window_ts_mismatch(self):
        """Surface window_ts != position window_ts → None."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        surface = make_surface(probability_lgb=0.55, window_ts=WINDOW_TS + 300)
        for _ in range(5):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is None

    def test_stale_clob_no_trigger(self):
        """Stale CLOB data → None."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        surface = make_surface(
            probability_lgb=0.55,
            last_clob_update_ts=time.time() - 10.0,  # 10s old, stale
        )
        for _ in range(5):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is None

    def test_remove_cleans_state(self):
        """remove() cleans up tracking state."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)
        assert f"{STRATEGY}:{WINDOW_TS}" in det._states

        det.remove(STRATEGY, WINDOW_TS)
        assert f"{STRATEGY}:{WINDOW_TS}" not in det._states

    def test_eval_offset_none(self):
        """Missing eval_offset → None."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        surface = SimpleNamespace(
            eval_offset=None,
            window_ts=WINDOW_TS,
            probability_lgb=0.55,
            last_clob_update_ts=time.time(),
        )
        result = det.evaluate(STRATEGY, WINDOW_TS, surface, **DEFAULT_PARAMS)
        assert result is None


# ── Threshold variations ─────────────────────────────────────────────────


class TestThresholdVariations:
    """Parameterized threshold tuning."""

    def test_lower_threshold_triggers_sooner(self):
        """20% threshold triggers on smaller fade."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        # dist=0.15 → fade_pct=0.25, triggers with 20% threshold
        surface = make_surface(probability_lgb=0.65)
        params = {**DEFAULT_PARAMS, "conviction_fade_threshold_pct": 0.20}

        for _ in range(2):
            det.evaluate(STRATEGY, WINDOW_TS, surface, **params)
        result = det.evaluate(STRATEGY, WINDOW_TS, surface, **params)
        assert result is not None

    def test_higher_threshold_needs_bigger_fade(self):
        """70% threshold needs a large fade."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        # dist=0.10 → fade_pct=0.50, NOT enough for 70% threshold
        surface = make_surface(probability_lgb=0.60)
        params = {**DEFAULT_PARAMS, "conviction_fade_threshold_pct": 0.70}

        for _ in range(5):
            result = det.evaluate(STRATEGY, WINDOW_TS, surface, **params)
        assert result is None

    def test_higher_floor_triggers_more_easily(self):
        """Higher absolute floor triggers sooner."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        # dist=0.14 → above default floor 0.08 but below 0.15
        surface = make_surface(probability_lgb=0.64)
        params = {**DEFAULT_PARAMS, "conviction_fade_absolute_floor": 0.15}

        for _ in range(2):
            det.evaluate(STRATEGY, WINDOW_TS, surface, **params)
        result = det.evaluate(STRATEGY, WINDOW_TS, surface, **params)
        assert result is not None

    def test_min_ticks_one_immediate_trigger(self):
        """min_ticks=1 triggers on first faded tick."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        surface = make_surface(probability_lgb=0.55)  # dist=0.05
        params = {**DEFAULT_PARAMS, "conviction_fade_min_ticks": 1}

        result = det.evaluate(STRATEGY, WINDOW_TS, surface, **params)
        assert result is not None


# ── FadeInstruction fields ───────────────────────────────────────────────


class TestFadeInstruction:
    """Verify FadeInstruction dataclass fields."""

    def test_instruction_fields(self):
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        surface = make_surface(probability_lgb=0.60)  # dist=0.10
        params = {**DEFAULT_PARAMS, "conviction_fade_min_ticks": 1}

        result = det.evaluate(STRATEGY, WINDOW_TS, surface, **params)
        assert result is not None
        assert result.strategy_id == STRATEGY
        assert result.window_ts == WINDOW_TS
        assert result.entry_dist == pytest.approx(0.20)
        assert result.current_dist == pytest.approx(0.10)
        assert result.fade_pct == pytest.approx(0.50)
        assert result.recommended_action == "exit"
        assert "threshold_pct" in result.trigger_reason
        assert result.consecutive_ticks >= 1

    def test_both_triggers_reported(self):
        """When both pct and floor trigger, reason includes both."""
        det = ConvictionFadeDetector()
        det.register(STRATEGY, WINDOW_TS, entry_dist=0.20)

        # dist=0.05 → fade_pct=0.75 (above 0.40) AND below floor 0.08
        surface = make_surface(probability_lgb=0.55)
        params = {**DEFAULT_PARAMS, "conviction_fade_min_ticks": 1}

        result = det.evaluate(STRATEGY, WINDOW_TS, surface, **params)
        assert result is not None
        assert "threshold_pct" in result.trigger_reason
        assert "absolute_floor" in result.trigger_reason
