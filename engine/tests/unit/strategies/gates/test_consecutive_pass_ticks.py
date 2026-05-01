"""Unit tests for ConsecutivePassTicksGate."""
from __future__ import annotations

import time
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from strategies.gates.consecutive_pass_ticks import (
    ConsecutivePassTicksGate,
    _MAX_GAP_SECONDS,
)


@dataclass
class _MockSurface:
    poly_direction: str = "UP"
    window_ts: int = 1777777200


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
    g = ConsecutivePassTicksGate(min_ticks=3)
    r = g.evaluate(_MockSurface(poly_direction=None))
    assert r.passed is False
    assert "not actionable" in r.reason


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
