"""Tier-lookup runtime override tests (PR #619 FIX 3).

Validates that gate_params.tier resolves through tickformer_tiers.yaml
to override the per-strategy default operating point, AND that an
explicit gate_params.up_threshold / eval_offset_remaining_* still
wins over the tier preset (operator > preset > default).
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

os.environ.setdefault(
    "DATABASE_URL", "postgresql://test:test@localhost:5432/test"
)

from strategies import gate_params as _gp  # noqa: E402
from strategies.configs import _tickformer_base  # noqa: E402
from strategies.configs.tickformer_v18_t180 import (  # noqa: E402
    evaluate_tickformer_v18_t180,
)
from strategies.configs.tickformer_v17_sniper import (  # noqa: E402
    evaluate_tickformer_v17_sniper,
)


@contextmanager
def _gp_active(params):
    token = _gp.set_active(params)
    try:
        yield
    finally:
        _gp.reset_active(token)


@pytest.fixture(autouse=True)
def _reset_state():
    _tickformer_base._consec_state.clear()
    _tickformer_base._TIERS_CACHE = None
    yield
    _tickformer_base._consec_state.clear()


def _surface(prob_field, p, eval_offset=120):
    ns = SimpleNamespace(
        asset="BTC",
        window_ts=1779000000,
        eval_offset=eval_offset,
        tickformer_trade_signal=None,
        clob_implied_up=0.70,
        fill_price=0.70,
    )
    setattr(ns, prob_field, p)
    return ns


def test_tier_a_narrow_band_skips_outside_band():
    """TIER_A is rem_max=60 → eval_offset=120 (remaining=180) skips."""
    surface = _surface("probability_tickformer_v18", 0.95, eval_offset=120)
    with _gp_active({"tier": "TIER_A", "shadow_only": 0}):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "outside_eval_offset_remaining_band"
    assert dec.metadata["eval_offset_remaining_max"] == 60
    assert dec.metadata["up_threshold"] == 0.85
    assert dec.metadata["tier"] == "TIER_A"


def test_tier_b_admits_120s_remaining_band():
    """TIER_B is rem_max=120, thr=0.90 → eval_offset=200 (rem=100) ok."""
    surface = _surface("probability_tickformer_v17", 0.92, eval_offset=200)
    with _gp_active({"tier": "TIER_B", "shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.metadata["up_threshold"] == 0.90
    assert dec.metadata["eval_offset_remaining_max"] == 120


def test_tier_c_default_band_thr_0_85():
    surface = _surface("probability_tickformer_v18", 0.86, eval_offset=120)
    with _gp_active({"tier": "TIER_C", "shadow_only": 0}):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.metadata["up_threshold"] == 0.85
    assert dec.metadata["eval_offset_remaining_max"] == 220


def test_explicit_up_threshold_overrides_tier_preset():
    """Operator's explicit gate_param wins over the tier preset."""
    surface = _surface("probability_tickformer_v18", 0.88, eval_offset=200)
    # TIER_B preset says up_threshold 0.90 / rem_max 120 — explicit
    # override of 0.85 + rem_max 220 wins.
    with _gp_active({
        "tier": "TIER_B",
        "up_threshold": 0.85,
        "eval_offset_remaining_max": 220,
        "shadow_only": 0,
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.metadata["up_threshold"] == 0.85


def test_unknown_tier_falls_back_to_per_strategy_defaults():
    """gate_params.tier='BOGUS' is silently ignored; defaults apply."""
    surface = _surface("probability_tickformer_v18", 0.95, eval_offset=120)
    with _gp_active({"tier": "BOGUS", "shadow_only": 0}):
        dec = evaluate_tickformer_v18_t180(surface)
    # Falls back to v18 default thr=0.90, rem_max=220.
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.metadata["up_threshold"] == 0.90
    assert dec.metadata["eval_offset_remaining_max"] == 220


def test_no_tier_key_uses_per_strategy_defaults():
    surface = _surface("probability_tickformer_v17", 0.95, eval_offset=180)
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert "tier" not in dec.metadata  # only added when tier is set
    assert dec.metadata["up_threshold"] == 0.85
    assert dec.metadata["eval_offset_remaining_max"] == 140
