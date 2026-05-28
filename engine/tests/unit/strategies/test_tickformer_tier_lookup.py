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
    _tickformer_base._DOWN_SYMMETRY_WARNED.clear()
    yield
    _tickformer_base._consec_state.clear()
    _tickformer_base._DOWN_SYMMETRY_WARNED.clear()


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
    """TIER_A is rem_max=60 → eval_offset=120 (remaining=120) skips.

    With the corrected formula remaining=eval_offset=120, which exceeds
    TIER_A's rem_max=60.
    """
    surface = _surface("probability_tickformer_v18", 0.95, eval_offset=120)
    with _gp_active({"tier": "TIER_A", "shadow_only": 0}):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "outside_eval_offset_remaining_band"
    assert dec.metadata["eval_offset_remaining_max"] == 60
    assert dec.metadata["up_threshold"] == 0.85
    assert dec.metadata["tier"] == "TIER_A"


def test_tier_b_admits_120s_remaining_band():
    """TIER_B is rem_max=120, thr=0.90 → eval_offset=100 (rem=100) ok.

    (Old eval_offset=200 was based on the inverted 300-eval_offset formula
    giving remaining=100. Corrected: eval_offset IS remaining, so use 100.)
    """
    surface = _surface("probability_tickformer_v17", 0.92, eval_offset=100)
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


def test_tier_preset_wins_over_explicit_gate_params():
    """Post-PR-#619 review: tier preset WINS over any YAML/runtime
    ``up_threshold`` / ``eval_offset_remaining_*`` value when set.

    This is the production-realistic case the original review caught:
    the three shipped YAMLs pre-populate ``up_threshold`` etc. — if
    YAML wins over tier, a runtime override flipping tier→TIER_A is
    a silent no-op.

    Operator workflow: to override a tier value, clear the ``tier``
    key in the runtime override AND set the explicit knob.

    eval_offset=100 → remaining=100, within TIER_B rem_max=120.
    (Old value was eval_offset=200 based on inverted 300-eval_offset formula.)
    """
    surface = _surface("probability_tickformer_v18", 0.92, eval_offset=100)
    # TIER_B preset says up_threshold 0.90 / rem_max 120. The
    # operator-set YAML-style ``up_threshold=0.85`` and
    # ``eval_offset_remaining_max=220`` are now IGNORED in favour
    # of the tier preset.
    with _gp_active({
        "tier": "TIER_B",
        "up_threshold": 0.85,
        "eval_offset_remaining_max": 220,
        "shadow_only": 0,
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    # rem_max 120 → eval_offset=100 (rem=100) is in-band; p=0.92 >= 0.90.
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.metadata["up_threshold"] == 0.90  # tier B
    assert dec.metadata["eval_offset_remaining_max"] == 120  # tier B


def test_tier_b_default_resolves_without_explicit_threshold():
    """Set tier: TIER_B in gate_params, no explicit up_threshold;
    verify it resolves to 0.90 (Tier B default). PR #619 review.

    eval_offset=100 → remaining=100, within TIER_B [60,120].
    (Old value was eval_offset=240 based on inverted formula giving rem=60.)
    """
    surface = _surface("probability_tickformer_v18", 0.91, eval_offset=100)
    with _gp_active({"tier": "TIER_B", "shadow_only": 0}):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.metadata["up_threshold"] == 0.90
    assert dec.metadata["eval_offset_remaining_min"] == 60
    assert dec.metadata["eval_offset_remaining_max"] == 120


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
    """No tier key → v17 defaults apply: thr=0.85, rem_max=140.

    eval_offset=120 → remaining=120, within v17 default [60,140].
    (Old value was eval_offset=180 which gave rem=120 via 300-eval_offset;
    with corrected formula eval_offset=180 → remaining=180 > rem_max=140.)
    """
    surface = _surface("probability_tickformer_v17", 0.95, eval_offset=120)
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert "tier" not in dec.metadata  # only added when tier is set
    assert dec.metadata["up_threshold"] == 0.85
    assert dec.metadata["eval_offset_remaining_max"] == 140
