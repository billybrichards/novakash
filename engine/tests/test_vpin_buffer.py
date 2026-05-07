"""
Tests for :class:`signals.vpin_buffer.VpinRollingBuffer`.

Mirrors the SQL semantics at `training/queries.py:379-382`:

    avg(vpin), stddev(vpin), min(vpin), max(vpin)

PostgreSQL's `STDDEV` is `STDDEV_SAMP` (n-1 denominator) — these tests
hand-compute the expected sample stddev to lock in that contract.
"""

from __future__ import annotations

import math
import statistics

import pytest

from signals.vpin_buffer import VpinRollingBuffer


# ─────────────────────────────────────────────────────────────────────
# Empty / under-sampled buffer
# ─────────────────────────────────────────────────────────────────────


def test_empty_buffer_returns_all_none():
    """No samples → all four stats are None (consistency rule)."""
    buf = VpinRollingBuffer(window_s=60)
    stats = buf.compute()
    assert stats == {
        "vpin_mean_60s": None,
        "vpin_std_60s": None,
        "vpin_min_60s": None,
        "vpin_max_60s": None,
    }
    assert len(buf) == 0


def test_single_sample_returns_all_none():
    """1 sample → all stats None: stddev is undefined and we apply the
    rule to the whole row for train/serve consistency."""
    buf = VpinRollingBuffer(window_s=60)
    buf.push(1000.0, 0.42)
    stats = buf.compute()
    assert stats == {
        "vpin_mean_60s": None,
        "vpin_std_60s": None,
        "vpin_min_60s": None,
        "vpin_max_60s": None,
    }
    assert len(buf) == 1


# ─────────────────────────────────────────────────────────────────────
# 2+ samples — hand-computed stats
# ─────────────────────────────────────────────────────────────────────


def test_two_samples_basic_stats():
    """2 samples → mean = avg, sample stddev (n-1), min/max correct."""
    buf = VpinRollingBuffer(window_s=60)
    buf.push(1000.0, 0.20)
    buf.push(1001.0, 0.40)

    stats = buf.compute()
    assert stats["vpin_mean_60s"] == pytest.approx(0.30, abs=1e-12)
    # Sample stddev of [0.2, 0.4]:
    #   mean = 0.3
    #   sq_dev = (0.2-0.3)^2 + (0.4-0.3)^2 = 0.01 + 0.01 = 0.02
    #   var = 0.02 / (2-1) = 0.02
    #   std = sqrt(0.02) ≈ 0.14142135623730953
    assert stats["vpin_std_60s"] == pytest.approx(math.sqrt(0.02), abs=1e-12)
    assert stats["vpin_min_60s"] == pytest.approx(0.20, abs=1e-12)
    assert stats["vpin_max_60s"] == pytest.approx(0.40, abs=1e-12)


def test_fixed_vector_matches_sql_semantics():
    """Hand-computed mean/stddev/min/max for a fixed vector — locks in
    STDDEV_SAMP semantics. statistics.stdev uses n-1 denominator and is
    the canonical Python equivalent of PostgreSQL STDDEV_SAMP."""
    buf = VpinRollingBuffer(window_s=60)
    samples = [
        (1000.0, 0.10),
        (1005.0, 0.25),
        (1010.0, 0.30),
        (1020.0, 0.45),
        (1030.0, 0.55),
        (1040.0, 0.60),
    ]
    for ts, v in samples:
        buf.push(ts, v)

    vals = [v for _, v in samples]
    expected_mean = sum(vals) / len(vals)
    expected_std = statistics.stdev(vals)  # n-1 denominator
    expected_min = min(vals)
    expected_max = max(vals)

    stats = buf.compute()
    assert stats["vpin_mean_60s"] == pytest.approx(expected_mean, abs=1e-12)
    assert stats["vpin_std_60s"] == pytest.approx(expected_std, abs=1e-12)
    assert stats["vpin_min_60s"] == pytest.approx(expected_min, abs=1e-12)
    assert stats["vpin_max_60s"] == pytest.approx(expected_max, abs=1e-12)


# ─────────────────────────────────────────────────────────────────────
# Eviction by time
# ─────────────────────────────────────────────────────────────────────


def test_eviction_keeps_only_recent_samples():
    """Push samples spanning > window_s; verify only recent are kept.

    With window_s=60 and the cutoff `newest - window_s`, a sample at
    ts=t is kept iff ts >= newest_ts - 60. The eviction check is
    strict-less-than (`< cutoff` evicts), so ts == cutoff is KEPT.
    """
    buf = VpinRollingBuffer(window_s=60)
    # Old samples — outside the window once the newest tick lands
    buf.push(1000.0, 0.10)
    buf.push(1010.0, 0.20)
    buf.push(1030.0, 0.30)  # will be cutoff exactly when newest=1090
    buf.push(1059.0, 0.40)
    # Newest sample — defines cutoff at 1090 - 60 = 1030
    buf.push(1090.0, 0.90)

    # Expect: 1000, 1010 evicted; 1030 kept (== cutoff); 1059, 1090 kept.
    assert len(buf) == 3
    stats = buf.compute()
    expected_vals = [0.30, 0.40, 0.90]
    assert stats["vpin_mean_60s"] == pytest.approx(sum(expected_vals) / 3, abs=1e-12)
    assert stats["vpin_min_60s"] == pytest.approx(0.30, abs=1e-12)
    assert stats["vpin_max_60s"] == pytest.approx(0.90, abs=1e-12)
    assert stats["vpin_std_60s"] == pytest.approx(statistics.stdev(expected_vals), abs=1e-12)


def test_eviction_handles_late_arriving_old_tick():
    """Time-warp tolerance: a stale tick arriving after a newer one
    must NOT flush the buffer (eviction uses the newest seen ts).
    """
    buf = VpinRollingBuffer(window_s=60)
    buf.push(2000.0, 0.50)
    buf.push(2010.0, 0.55)
    # Stale replay tick — should be appended but not cause eviction of
    # the newer ts=2010 sample.
    buf.push(1500.0, 0.10)

    assert len(buf) == 3
    stats = buf.compute()
    assert stats["vpin_min_60s"] == pytest.approx(0.10, abs=1e-12)
    assert stats["vpin_max_60s"] == pytest.approx(0.55, abs=1e-12)


# ─────────────────────────────────────────────────────────────────────
# None / NaN / inf VPIN handling
# ─────────────────────────────────────────────────────────────────────


def test_none_vpin_is_skipped():
    """None vpin = no measurement — push must not raise and not store."""
    buf = VpinRollingBuffer(window_s=60)
    buf.push(1000.0, None)
    buf.push(1001.0, None)
    assert len(buf) == 0
    assert buf.compute()["vpin_mean_60s"] is None


def test_nan_and_inf_vpin_are_skipped():
    """NaN/inf vpins are dropped — same as None."""
    buf = VpinRollingBuffer(window_s=60)
    buf.push(1000.0, float("nan"))
    buf.push(1001.0, float("inf"))
    buf.push(1002.0, float("-inf"))
    buf.push(1003.0, 0.5)
    buf.push(1004.0, 0.7)
    assert len(buf) == 2
    stats = buf.compute()
    assert stats["vpin_mean_60s"] == pytest.approx(0.6, abs=1e-12)


# ─────────────────────────────────────────────────────────────────────
# Memory cap (defensive maxlen)
# ─────────────────────────────────────────────────────────────────────


def test_maxlen_caps_buffer_under_replay_storm():
    """Push many same-timestamp samples (eviction by ts is a no-op) and
    verify the deque never exceeds the maxlen cap of 256."""
    buf = VpinRollingBuffer(window_s=60)
    for i in range(10_000):
        # All ts == 5000.0, so time-eviction does nothing — only maxlen
        # protects against unbounded growth.
        buf.push(5000.0, 0.1 + (i % 100) * 0.001)
    assert len(buf) <= buf.maxlen == 256


def test_maxlen_under_normal_1hz_load():
    """Under the expected ~1 tick/sec × 60s pattern, the buffer
    converges to ~60 samples, well under the 256 cap."""
    buf = VpinRollingBuffer(window_s=60)
    max_observed = 0
    for i in range(600):  # 10 minutes worth of 1 Hz ticks
        buf.push(float(i), 0.3 + (i % 7) * 0.01)
        max_observed = max(max_observed, len(buf))
    # Expect steady-state around 60-61 samples.
    assert max_observed <= 65
    assert max_observed >= 55
