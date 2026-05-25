"""Unit tests for ConsecutivePassTicksGate.

Includes regression tests for Hub #546 (2026-05-25):
  15m classifier strategies have poly_direction=None because 15m /v4/snapshot
  payloads don't include the polymarket_live_recommended_outcome block.  The
  gate now falls back to deriving direction from probability_classifier so the
  four v_eth/v_xrp × top10/top20 classifier strategies can actually fire.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import patch

import pytest

from strategies.gates.consecutive_pass_ticks import (
    ConsecutivePassTicksGate,
    _MAX_GAP_SECONDS,
)


@dataclass
class _MockSurface:
    poly_direction: Optional[str] = "UP"
    window_ts: int = 1777777200
    probability_classifier: Optional[float] = None


def test_init_rejects_non_positive_min_ticks():
    with pytest.raises(ValueError):
        ConsecutivePassTicksGate(min_ticks=0)
    with pytest.raises(ValueError):
        ConsecutivePassTicksGate(min_ticks=-1)


def test_first_eval_returns_count_1_fails():
    g = ConsecutivePassTicksGate(min_ticks=12)
    r = g.evaluate(_MockSurface())
    assert r.passed is False
    assert r.data["count"] == 1
    assert "1/12" in r.reason


def test_consecutive_evals_same_direction_increment_count():
    g = ConsecutivePassTicksGate(min_ticks=12)
    s = _MockSurface(poly_direction="UP", window_ts=1)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        for i in range(1, 12):
            r = g.evaluate(s)
            assert r.data["count"] == i
            assert r.passed is False
            fixed_time[0] += 2.0  # simulate 2s tick interval

        # 12th evaluation -- should pass
        r = g.evaluate(s)
        assert r.data["count"] == 12
        assert r.passed is True


def test_pass_persists_after_threshold():
    g = ConsecutivePassTicksGate(min_ticks=3)
    s = _MockSurface(poly_direction="UP", window_ts=1)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        # First 3 ticks
        for _ in range(3):
            r = g.evaluate(s)
            fixed_time[0] += 2.0
        assert r.passed is True

        # 4th and 5th -- still passing, count keeps growing
        r = g.evaluate(s)
        fixed_time[0] += 2.0
        assert r.passed is True
        assert r.data["count"] == 4

        r = g.evaluate(s)
        assert r.passed is True
        assert r.data["count"] == 5


def test_direction_change_resets_counter():
    g = ConsecutivePassTicksGate(min_ticks=3)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        # Two UP ticks
        s_up = _MockSurface(poly_direction="UP", window_ts=1)
        g.evaluate(s_up); fixed_time[0] += 2.0
        r = g.evaluate(s_up)
        assert r.data["count"] == 2

        # Direction flips DOWN -- counter resets to 1
        fixed_time[0] += 2.0
        s_dn = _MockSurface(poly_direction="DOWN", window_ts=1)
        r = g.evaluate(s_dn)
        assert r.data["count"] == 1
        assert r.data["direction"] == "DOWN"


def test_gap_too_large_resets_counter():
    """Upstream gate failure between evaluations should reset the counter.

    This is the core robustness property: if direction is UP for tick A,
    then an upstream gate fails for several ticks (this gate isn't called),
    then direction is UP again, the count must restart -- otherwise we'd
    falsely accept N intermittent passes as N consecutive ones.
    """
    g = ConsecutivePassTicksGate(min_ticks=3)
    s = _MockSurface(poly_direction="UP", window_ts=1)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        # Two consecutive ticks
        g.evaluate(s); fixed_time[0] += 2.0
        r = g.evaluate(s)
        assert r.data["count"] == 2

        # Skip ahead 10s (simulating upstream gate failures for ~5 ticks)
        fixed_time[0] += 10.0
        r = g.evaluate(s)
        # Counter resets -- gap > _MAX_GAP_SECONDS (5s)
        assert r.data["count"] == 1


def test_max_gap_boundary():
    """Gap exactly == _MAX_GAP_SECONDS should NOT reset (<= comparison)."""
    g = ConsecutivePassTicksGate(min_ticks=3)
    s = _MockSurface(poly_direction="UP", window_ts=1)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        g.evaluate(s)
        fixed_time[0] += _MAX_GAP_SECONDS  # exactly the cap
        r = g.evaluate(s)
        assert r.data["count"] == 2  # still consecutive

        # Just over the cap -- resets
        fixed_time[0] += _MAX_GAP_SECONDS + 0.1
        r = g.evaluate(s)
        assert r.data["count"] == 1


def test_different_window_ts_independent_counters():
    g = ConsecutivePassTicksGate(min_ticks=3)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        # Window A: 2 ticks
        s_a = _MockSurface(poly_direction="UP", window_ts=1)
        g.evaluate(s_a); fixed_time[0] += 2.0
        r = g.evaluate(s_a)
        assert r.data["count"] == 2

        # Window B: independent counter
        fixed_time[0] += 2.0
        s_b = _MockSurface(poly_direction="UP", window_ts=2)
        r = g.evaluate(s_b)
        assert r.data["count"] == 1


def test_invalid_direction_returns_failed():
    # poly_direction=None AND probability_classifier=None → both unavailable
    g = ConsecutivePassTicksGate(min_ticks=3)
    r = g.evaluate(_MockSurface(poly_direction=None, probability_classifier=None))
    assert r.passed is False
    assert "not actionable" in r.reason


# ── Hub #546 regression: classifier fallback when poly_direction is None ──


def test_classifier_fallback_up_when_poly_direction_none():
    """prob_classifier > 0.5 → direction=UP when poly_direction is None."""
    g = ConsecutivePassTicksGate(min_ticks=3)
    s = _MockSurface(poly_direction=None, window_ts=1, probability_classifier=0.80)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        for _ in range(3):
            r = g.evaluate(s)
            fixed_time[0] += 2.0
    assert r.passed is True
    assert r.data["direction"] == "UP"


def test_classifier_fallback_down_when_poly_direction_none():
    """prob_classifier < 0.5 → direction=DOWN when poly_direction is None."""
    g = ConsecutivePassTicksGate(min_ticks=3)
    s = _MockSurface(poly_direction=None, window_ts=2, probability_classifier=0.18)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        for _ in range(3):
            r = g.evaluate(s)
            fixed_time[0] += 2.0
    assert r.passed is True
    assert r.data["direction"] == "DOWN"


def test_classifier_exactly_half_still_not_actionable():
    """prob_classifier == 0.5 is ambiguous — gate must not pick a direction."""
    g = ConsecutivePassTicksGate(min_ticks=3)
    r = g.evaluate(_MockSurface(poly_direction=None, probability_classifier=0.5))
    assert r.passed is False
    assert "not actionable" in r.reason


def test_poly_direction_wins_over_classifier():
    """When poly_direction is valid it must be used regardless of classifier."""
    g = ConsecutivePassTicksGate(min_ticks=3)
    # poly says DOWN but classifier says UP (prob=0.80)
    s = _MockSurface(poly_direction="DOWN", window_ts=3, probability_classifier=0.80)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        for _ in range(3):
            r = g.evaluate(s)
            fixed_time[0] += 2.0
    assert r.passed is True
    assert r.data["direction"] == "DOWN"  # poly wins


def test_classifier_direction_flip_resets_counter():
    """Classifier-derived direction flipping (UP→DOWN) resets the counter."""
    g = ConsecutivePassTicksGate(min_ticks=3)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        s_up = _MockSurface(poly_direction=None, window_ts=4, probability_classifier=0.80)
        g.evaluate(s_up); fixed_time[0] += 2.0
        r = g.evaluate(s_up)
        assert r.data["count"] == 2

        # Classifier flips to DOWN
        fixed_time[0] += 2.0
        s_dn = _MockSurface(poly_direction=None, window_ts=4, probability_classifier=0.20)
        r = g.evaluate(s_dn)
        assert r.data["count"] == 1
        assert r.data["direction"] == "DOWN"


def test_missing_window_ts_returns_failed():
    g = ConsecutivePassTicksGate(min_ticks=3)
    r = g.evaluate(_MockSurface(window_ts=0))
    assert r.passed is False
    assert "window_ts missing" in r.reason


def test_two_gate_instances_have_independent_state():
    """Two strategies registering this gate must NOT share counter state."""
    g1 = ConsecutivePassTicksGate(min_ticks=3)
    g2 = ConsecutivePassTicksGate(min_ticks=3)
    s = _MockSurface(poly_direction="UP", window_ts=1)
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        # g1: 2 ticks
        g1.evaluate(s); fixed_time[0] += 2.0
        r1 = g1.evaluate(s)
        assert r1.data["count"] == 2

        # g2: untouched -- first eval is still count=1
        r2 = g2.evaluate(s)
        assert r2.data["count"] == 1


def test_state_cleanup_when_oversized():
    """After many distinct windows, old state should be dropped."""
    from strategies.gates.consecutive_pass_ticks import _CLEANUP_AT_SIZE

    g = ConsecutivePassTicksGate(min_ticks=3)
    base_window = 1_700_000_000  # arbitrary epoch
    fixed_time = [1000.0]
    with patch("time.time", side_effect=lambda: fixed_time[0]):
        # Populate enough state to trigger cleanup
        for i in range(_CLEANUP_AT_SIZE + 5):
            s = _MockSurface(poly_direction="UP", window_ts=base_window + i * 900)
            g.evaluate(s)
            fixed_time[0] += 2.0
    # State should be bounded -- cleanup drops entries older than 2 windows
    # relative to most-recently-seen window_ts.
    assert len(g._state) <= _CLEANUP_AT_SIZE + 5
