"""B2 fix tests: ChainlinkFreshnessGate default 60s + edge cases.

Audit #374 (2026-05-06). Verifies:
* Default max_age_seconds is 60 (NOT 30 — old default was too tight for the
  ~27s Polygon Chainlink heartbeat plus RPC jitter).
* 59s age passes the default gate.
* 60s age passes the default gate.
* 61s age is skipped.
* Custom override (max_age_seconds=30) still works for stricter strategies.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from strategies.gates.chainlink_freshness import ChainlinkFreshnessGate


def _surface(age_seconds=None):
    return SimpleNamespace(delta_chainlink_age_seconds=age_seconds)


# ─────────────── B2: default is now 60s ─────────────────────────────────────


def test_default_max_age_is_60():
    """Gate constructor default must be 60s, not the old 30s."""
    gate = ChainlinkFreshnessGate()
    assert gate._max_age_seconds == 60


def test_59s_passes_default():
    gate = ChainlinkFreshnessGate()
    res = gate.evaluate(_surface(age_seconds=59))
    assert res.passed


def test_60s_passes_default():
    """60s should still pass — it is AT the limit, not over."""
    gate = ChainlinkFreshnessGate()
    res = gate.evaluate(_surface(age_seconds=60))
    assert res.passed


def test_61s_skips_default():
    """61s is beyond the 60s ceiling — gate should SKIP."""
    gate = ChainlinkFreshnessGate()
    res = gate.evaluate(_surface(age_seconds=61))
    assert not res.passed
    assert "stale" in res.reason.lower() or ">" in res.reason


def test_unknown_age_passes_by_default():
    """surface.delta_chainlink_age_seconds=None → pass (backward-compat)."""
    gate = ChainlinkFreshnessGate()
    res = gate.evaluate(_surface(age_seconds=None))
    assert res.passed


def test_unknown_age_skips_when_skip_when_unknown():
    """skip_when_unknown=True → fail when age is None."""
    gate = ChainlinkFreshnessGate(skip_when_unknown=True)
    res = gate.evaluate(_surface(age_seconds=None))
    assert not res.passed


def test_custom_30s_override_still_works():
    """Strategies that want stricter freshness can still set max_age=30."""
    gate = ChainlinkFreshnessGate(max_age_seconds=30)
    assert gate._max_age_seconds == 30
    res_pass = gate.evaluate(_surface(age_seconds=29))
    assert res_pass.passed
    res_skip = gate.evaluate(_surface(age_seconds=31))
    assert not res_skip.passed
