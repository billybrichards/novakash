"""Tests for direction-split eval-band support in _tickformer_base (plan #760).

Covers:
  1. Symmetric band (no direction-specific keys) — back-compat, both UP and
     DOWN use the same rem_min/rem_max.
  2. Only _max_down set — applied only to DOWN; UP still uses symmetric max.
  3. Only _max_up set — applied only to UP; DOWN still uses symmetric max.
  4. Both direction-specific keys set — each applied to its own direction.
  5. _min_ variant: only _min_down tightens the DOWN floor.
  6. Direction UP uses UP band, direction DOWN uses DOWN band (end-to-end).
  7. Runtime override _max_down=140, tick at t=170 → SKIP for DOWN, TRADE
     for UP (back-compat: UP still passes the 0-180 symmetric max).
  8. back-compat: existing runtime override with only bare key keeps working.
  9. _resolve_band_for_direction unit: returns sym when no direction-specific key.
 10. _resolve_band_for_direction unit: direction key wins over sym.

Plan note: RDS public.notes id=760.
"""
from __future__ import annotations

import os
import sys
import time
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
from strategies.configs._tickformer_base import _resolve_band_for_direction  # noqa: E402
from strategies.configs.tickformer_v18_t180 import (  # noqa: E402
    evaluate_tickformer_v18_t180,
)
from strategies.configs.tickformer_v17_sniper import (  # noqa: E402
    evaluate_tickformer_v17_sniper,
)
from strategies.configs.tickformer_v16_pure import (  # noqa: E402
    evaluate_tickformer_v16_pure,
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
    _tickformer_base._DOWN_SYMMETRY_WARNED.discard("tickformer_v16_pure")
    _tickformer_base._DOWN_SYMMETRY_WARNED.discard("tickformer_v17_sniper")
    _tickformer_base._DOWN_SYMMETRY_WARNED.discard("tickformer_v18_t180")
    yield
    _tickformer_base._consec_state.clear()


def _surface(
    prob_field: str,
    prob_value,
    *,
    eval_offset: int = 120,
    trade_signal=None,
    asset: str = "BTC",
    window_ts: int = 1779000000,
    fill_price: float = 0.75,
):
    """Build a minimal surface suitable for tickformer evaluation."""
    ns = SimpleNamespace(
        asset=asset,
        eval_offset=eval_offset,
        tickformer_trade_signal=trade_signal,
        clob_implied_up=fill_price,
        fill_price=fill_price,
    )
    setattr(ns, prob_field, prob_value)
    # Unique window_ts per call so consec counters never bleed across tests.
    ns.window_ts = window_ts + int(time.time_ns() % 1_000_000)
    return ns


# ── Unit tests for _resolve_band_for_direction ───────────────────────────


def test_resolve_band_no_direction_key_returns_symmetric():
    """When no direction-specific keys are in the active bag, sym values returned."""
    with _gp_active({"shadow_only": 0, "eval_offset_remaining_min": 0, "eval_offset_remaining_max": 180}):
        up_min, up_max = _resolve_band_for_direction("UP", sym_min=0, sym_max=180)
        dn_min, dn_max = _resolve_band_for_direction("DOWN", sym_min=0, sym_max=180)
    assert (up_min, up_max) == (0, 180)
    assert (dn_min, dn_max) == (0, 180)


def test_resolve_band_direction_key_wins_over_sym():
    """eval_offset_remaining_max_down=140 overrides sym_max=180 for DOWN only."""
    with _gp_active({
        "shadow_only": 0,
        "eval_offset_remaining_max": 180,
        "eval_offset_remaining_max_down": 140,
    }):
        up_min, up_max = _resolve_band_for_direction("UP", sym_min=0, sym_max=180)
        dn_min, dn_max = _resolve_band_for_direction("DOWN", sym_min=0, sym_max=180)
    assert (up_min, up_max) == (0, 180)  # UP unchanged
    assert (dn_min, dn_max) == (0, 140)  # DOWN tightened


def test_resolve_band_up_key_overrides_up_only():
    """eval_offset_remaining_max_up=160 overrides UP only; DOWN stays at sym."""
    with _gp_active({
        "eval_offset_remaining_max_up": 160,
    }):
        up_min, up_max = _resolve_band_for_direction("UP", sym_min=0, sym_max=180)
        dn_min, dn_max = _resolve_band_for_direction("DOWN", sym_min=0, sym_max=180)
    assert up_max == 160
    assert dn_max == 180


def test_resolve_band_both_direction_keys_set():
    """Both _up and _down keys set — each direction uses its own value."""
    with _gp_active({
        "eval_offset_remaining_max_up": 200,
        "eval_offset_remaining_max_down": 120,
        "eval_offset_remaining_min_up": 10,
        "eval_offset_remaining_min_down": 20,
    }):
        up_min, up_max = _resolve_band_for_direction("UP", sym_min=0, sym_max=180)
        dn_min, dn_max = _resolve_band_for_direction("DOWN", sym_min=0, sym_max=180)
    assert (up_min, up_max) == (10, 200)
    assert (dn_min, dn_max) == (20, 120)


# ── End-to-end: UP uses UP band, DOWN uses DOWN band ────────────────────


def test_up_uses_up_band_down_uses_down_band_independently():
    """t=170s: DOWN band 0-140 rejects DOWN; UP band 0-180 accepts UP."""
    # v18: up_threshold=0.90, down_threshold=0.10.
    # eval_offset=170 → remaining=170.
    # UP: 170 is inside 0-180 → TRADE (shadow off).
    # DOWN: 170 is outside 0-140 → SKIP outside_band_DOWN.

    # --- UP side ---
    surface_up = _surface(
        "probability_tickformer_v18",
        0.93,  # p >= 0.90 → UP
        eval_offset=170,
        fill_price=0.75,
    )
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "eval_offset_remaining_max_down": 140,  # DOWN tightened
    }):
        dec_up = evaluate_tickformer_v18_t180(surface_up)
    assert dec_up.action == "TRADE", dec_up.skip_reason
    assert dec_up.direction == "UP"
    assert dec_up.metadata.get("band_max") == 180

    # --- DOWN side ---
    surface_dn = _surface(
        "probability_tickformer_v18",
        0.05,  # p <= 0.10 → DOWN
        eval_offset=170,
        fill_price=0.20,
    )
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "eval_offset_remaining_max_down": 140,  # DOWN tightened
    }):
        dec_dn = evaluate_tickformer_v18_t180(surface_dn)
    assert dec_dn.action == "SKIP"
    assert "outside_band_DOWN" in dec_dn.skip_reason, dec_dn.skip_reason
    assert dec_dn.metadata.get("band_max") == 140


def test_down_in_band_trades_when_down_band_is_wide():
    """DOWN at t=170s is IN band when _max_down=200 — should TRADE."""
    surface = _surface(
        "probability_tickformer_v18",
        0.05,
        eval_offset=170,
        fill_price=0.20,
    )
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "eval_offset_remaining_max_down": 200,
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "DOWN"
    assert dec.metadata.get("band_max") == 200


# ── Back-compat: only symmetric band set ────────────────────────────────


def test_symmetric_only_band_both_directions_use_it():
    """No direction-specific keys: both UP and DOWN use the same band."""
    # v18 YAML defaults: min=60, max=220. eval_offset=100 → remaining=100, in band.
    surface_up = _surface(
        "probability_tickformer_v18",
        0.93,
        eval_offset=100,
        fill_price=0.75,
    )
    surface_dn = _surface(
        "probability_tickformer_v18",
        0.05,
        eval_offset=100,
        fill_price=0.20,
    )
    with _gp_active({"shadow_only": 0}):
        dec_up = evaluate_tickformer_v18_t180(surface_up)
        dec_dn = evaluate_tickformer_v18_t180(surface_dn)
    assert dec_up.action == "TRADE", dec_up.skip_reason
    assert dec_up.direction == "UP"
    assert dec_dn.action == "TRADE", dec_dn.skip_reason
    assert dec_dn.direction == "DOWN"


def test_back_compat_existing_symmetric_runtime_override():
    """Existing override with only bare eval_offset_remaining_max — both dirs use it."""
    # This is the pattern every existing strategy_runtime_overrides row uses today.
    surface_up = _surface(
        "probability_tickformer_v18",
        0.93,
        eval_offset=150,
        fill_price=0.75,
    )
    surface_dn = _surface(
        "probability_tickformer_v18",
        0.05,
        eval_offset=150,
        fill_price=0.20,
    )
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,  # bare key — no direction-specific key
    }):
        dec_up = evaluate_tickformer_v18_t180(surface_up)
        dec_dn = evaluate_tickformer_v18_t180(surface_dn)
    assert dec_up.action == "TRADE", dec_up.skip_reason
    assert dec_dn.action == "TRADE", dec_dn.skip_reason
    assert dec_up.metadata.get("band_max") == 180
    assert dec_dn.metadata.get("band_max") == 180


# ── Only _min direction-specific key set ────────────────────────────────


def test_only_min_down_set_tightens_down_floor():
    """eval_offset_remaining_min_down=50 raises DOWN floor; UP floor stays at 0."""
    # eval_offset=30 → remaining=30.
    # UP: floor 0, max 180 → 30 in band → TRADE.
    # DOWN: floor 50, max 180 → 30 outside band → SKIP.

    surface_up = _surface(
        "probability_tickformer_v18",
        0.93,
        eval_offset=30,
        fill_price=0.75,
    )
    surface_dn = _surface(
        "probability_tickformer_v18",
        0.05,
        eval_offset=30,
        fill_price=0.20,
    )
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "eval_offset_remaining_min_down": 50,  # DOWN floor raised
    }):
        dec_up = evaluate_tickformer_v18_t180(surface_up)
        dec_dn = evaluate_tickformer_v18_t180(surface_dn)

    assert dec_up.action == "TRADE", dec_up.skip_reason
    assert dec_up.metadata.get("band_min") == 0

    assert dec_dn.action == "SKIP"
    assert "outside_band_DOWN" in dec_dn.skip_reason, dec_dn.skip_reason
    assert dec_dn.metadata.get("band_min") == 50


# ── Incidentally: skip_reason granularity ────────────────────────────────


def test_skip_reason_contains_direction_and_band_limits():
    """Skip reason encodes the direction and the band limits for log analysis."""
    surface = _surface(
        "probability_tickformer_v18",
        0.05,  # DOWN
        eval_offset=170,  # outside DOWN band 0-140
        fill_price=0.20,
    )
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "eval_offset_remaining_max_down": 140,
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "SKIP"
    assert "DOWN" in dec.skip_reason
    assert "140" in dec.skip_reason


def test_skip_reason_up_outside_band():
    """Skip reason encodes UP when UP is the out-of-band direction."""
    surface = _surface(
        "probability_tickformer_v18",
        0.93,  # UP
        eval_offset=10,  # outside band (rem_min=60 by default for v18)
        fill_price=0.75,
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "SKIP"
    assert "UP" in dec.skip_reason


# ── Scenario from plan #760 motivation ──────────────────────────────────


def test_plan_760_scenario_v18_t170_down_outside_tighter_band():
    """Plan #760 motivating case: v18 UP wants 0-180, DOWN wants 0-140.

    At t=170s (170s remaining):
      - UP p=0.93 → in band 0-180 → TRADE UP.
      - DOWN p=0.05 → in band 0-140? NO (170 > 140) → SKIP outside_band_DOWN.

    This is the exact scenario that triggered the v18 21:00 UTC loss.
    """
    surface_up = _surface(
        "probability_tickformer_v18", 0.93, eval_offset=170, fill_price=0.75
    )
    surface_dn = _surface(
        "probability_tickformer_v18", 0.05, eval_offset=170, fill_price=0.20
    )
    params = {
        "shadow_only": 0,
        "up_threshold": 0.80,
        "down_threshold": 0.42,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "eval_offset_remaining_max_down": 140,
    }
    with _gp_active(params):
        dec_up = evaluate_tickformer_v18_t180(surface_up)
    with _gp_active(params):
        dec_dn = evaluate_tickformer_v18_t180(surface_dn)

    assert dec_up.action == "TRADE", dec_up.skip_reason
    assert dec_up.direction == "UP"

    assert dec_dn.action == "SKIP"
    assert "outside_band_DOWN" in dec_dn.skip_reason, dec_dn.skip_reason
