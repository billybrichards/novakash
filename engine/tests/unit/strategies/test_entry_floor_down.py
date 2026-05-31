"""Unit tests for the entry_floor_down gate (RDS notes #771/#772 — fill-asymmetry fix).

entry_floor_down is the symmetric counterpart to entry_floor_up:

  entry_floor_up  — blocks UP fires when YES fill < floor (deep-contrarian UP).
  entry_floor_down — blocks DOWN fires when NO fill < floor (deep-contrarian DOWN).

Default value: None (gate is OFF — full back-compat, no existing behaviour changes).

Covers:
  1. test_entry_floor_down_blocks_low_fill_when_set
       entry_floor_down=0.70, down_fill=0.65 → SKIP with entry_floor_down reason
  2. test_entry_floor_down_allows_high_fill_when_set
       entry_floor_down=0.70, down_fill=0.75 → TRADE (no skip)
  3. test_entry_floor_down_disabled_when_none
       entry_floor_down=None (default) → no skip regardless of fill
  4. test_entry_floor_down_does_not_affect_up_direction
       entry_floor_down=0.90 set, direction=UP, UP fill=0.30 → passes floor_down check
       (entry_floor_up still independently blocks it when below that threshold)
  5. test_entry_floor_down_exact_boundary_allowed
       floor=0.70, down_fill exactly 0.70 → TRADE (strictly less-than semantics)
  6. test_entry_floor_down_skip_reason_contains_values
       skip reason must include the fill value and the threshold
  7. test_entry_floor_down_meta_key_present
       entry_floor_down is recorded in metadata when set
  8. test_entry_floor_down_meta_key_none_when_unset
       entry_floor_down metadata is None when not set in gate_params

All tests use tickformer_v18_t180 (canonical variant) with shadow_only=0.
The LGB combo variants are covered via test_entry_floor_down_lgb_combo.
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
from strategies.configs.tickformer_v18_t180 import (  # noqa: E402
    evaluate_tickformer_v18_t180,
)
from strategies.configs.v9_2_v12_combo_pure import (  # noqa: E402
    evaluate_v9_2_v12_combo_pure,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

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
    yield
    _tickformer_base._consec_state.clear()


def _tickformer_surface(
    *,
    prob: float = 0.05,       # p < 0.10 → DOWN
    eval_offset: int = 120,
    clob_implied_up: float = 0.30,   # YES leg
    clob_down_ask: float = 0.65,     # NO/DOWN leg — tickformer reads this
    window_ts: int = 1779000000,
):
    """Build a minimal surface for tickformer DOWN tests.

    tickformer reads clob_down_ask as the down_fill_price directly.
    """
    ns = SimpleNamespace(
        asset="BTC",
        eval_offset=eval_offset,
        tickformer_trade_signal=None,
        clob_implied_up=clob_implied_up,
        fill_price=clob_implied_up,
        clob_down_ask=clob_down_ask,
        probability_tickformer_v18=prob,
    )
    ns.window_ts = window_ts + int(time.time_ns() % 1_000_000)
    return ns


def _lgb_combo_surface(
    *,
    p_v92: float = 0.15,   # below 0.50 → DOWN
    p_v12: float = 0.15,
    eval_offset: int = 120,
    fill_price: float = 0.35,  # YES/UP leg (NO proxy = 1 - 0.35 = 0.65)
    window_ts: int = 1779000000,
):
    """Minimal surface for v9_2_v12_combo_pure DOWN tests.

    These LGB files use fill_price=YES leg; NO proxy = 1 - fill_price.
    fill_price=0.35 → NO proxy=0.65.
    """
    from strategies.data_surface import FullDataSurface

    # We use SimpleNamespace since FullDataSurface requires many fields and
    # combo_pure uses getattr with defaults for everything it needs.
    ns = SimpleNamespace(
        asset="ETH",
        eval_offset=eval_offset,
        fill_price=fill_price,
        clob_implied_up=fill_price,
        probability_lgb_v9_2_pure=p_v92,
        probability_lgb_v12_pure=p_v12,
    )
    ns.window_ts = window_ts + int(time.time_ns() % 1_000_000)
    return ns


# ── Tickformer entry_floor_down tests ─────────────────────────────────────────


def test_entry_floor_down_blocks_low_fill_when_set():
    """entry_floor_down=0.70, clob_down_ask=0.65 → SKIP with entry_floor_down reason.

    Scenario: model strongly predicts DOWN (p=0.05), but the NO leg is priced
    very low (0.65), meaning YES is 0.35 — deep-contrarian bet that historically
    collapses WR (notes #771/#772 LOW bucket: 0-33% WR).
    """
    surface = _tickformer_surface(prob=0.05, clob_down_ask=0.65, clob_implied_up=0.35)
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "entry_floor_down": 0.70,
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "SKIP", f"expected SKIP, got {dec.action} (reason: {dec.skip_reason})"
    assert "entry_floor_down" in dec.skip_reason, dec.skip_reason
    assert "0.650" in dec.skip_reason, dec.skip_reason


def test_entry_floor_down_allows_high_fill_when_set():
    """entry_floor_down=0.70, clob_down_ask=0.75 → TRADE (NO fill is above floor).

    Scenario: model predicts DOWN, NO leg is 0.75 — market is pricing a real
    probability of DOWN, fill is above the contrarian threshold.
    """
    surface = _tickformer_surface(prob=0.05, clob_down_ask=0.75, clob_implied_up=0.25)
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "entry_floor_down": 0.70,
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE", f"expected TRADE, got {dec.action} (reason: {dec.skip_reason})"
    assert dec.direction == "DOWN"


def test_entry_floor_down_disabled_when_none():
    """entry_floor_down=None (default) → no skip — full back-compat.

    Any existing strategy that does not set entry_floor_down must be unaffected.
    """
    surface = _tickformer_surface(prob=0.05, clob_down_ask=0.40, clob_implied_up=0.60)
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        # entry_floor_down intentionally absent → defaults to None
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE", f"expected TRADE, got {dec.action} (reason: {dec.skip_reason})"
    assert dec.direction == "DOWN"


def test_entry_floor_down_does_not_affect_up_direction():
    """entry_floor_down set high, but direction=UP → floor_down check never fires.

    UP fires should only be controlled by entry_floor_up, not entry_floor_down.
    Set entry_floor_up=0.0 (permissive) so UP can fire freely.
    """
    # p=0.95 → UP (above up_threshold=0.90)
    surface = _tickformer_surface(prob=0.95, clob_down_ask=0.05, clob_implied_up=0.80)
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "entry_floor_up": 0.0,    # permissive — UP always allowed
        "entry_floor_down": 0.99, # aggressive — would block all DOWN (but we're UP)
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE", f"expected TRADE, got {dec.action} (reason: {dec.skip_reason})"
    assert dec.direction == "UP"


def test_entry_floor_down_exact_boundary_allowed():
    """entry_floor_down=0.70, clob_down_ask=0.70 exactly → TRADE (strictly less-than).

    The check is `down_fill_price < entry_floor_down`, so an exact match passes.
    """
    surface = _tickformer_surface(prob=0.05, clob_down_ask=0.70, clob_implied_up=0.30)
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "entry_floor_down": 0.70,
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE", f"expected TRADE at exact boundary, got {dec.action} (reason: {dec.skip_reason})"
    assert dec.direction == "DOWN"


def test_entry_floor_down_skip_reason_contains_values():
    """Skip reason includes both the fill value and the threshold for log analysis."""
    surface = _tickformer_surface(prob=0.05, clob_down_ask=0.55, clob_implied_up=0.45)
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "entry_floor_down": 0.70,
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "SKIP"
    assert "entry_floor_down" in dec.skip_reason
    # Must contain both the actual fill value and the threshold
    assert "0.550" in dec.skip_reason, f"fill value missing from: {dec.skip_reason}"
    assert "0.7" in dec.skip_reason, f"threshold missing from: {dec.skip_reason}"


def test_entry_floor_down_meta_key_present_when_set():
    """entry_floor_down is recorded in decision metadata when set."""
    surface = _tickformer_surface(prob=0.05, clob_down_ask=0.75, clob_implied_up=0.25)
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        "entry_floor_down": 0.70,
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE"
    assert "entry_floor_down" in dec.metadata, "entry_floor_down missing from metadata"
    assert dec.metadata["entry_floor_down"] == 0.70


def test_entry_floor_down_meta_key_none_when_unset():
    """entry_floor_down metadata is None when not configured (default OFF state)."""
    surface = _tickformer_surface(prob=0.05, clob_down_ask=0.75, clob_implied_up=0.25)
    with _gp_active({
        "shadow_only": 0,
        "up_threshold": 0.90,
        "down_threshold": 0.10,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 180,
        # no entry_floor_down
    }):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE"
    assert "entry_floor_down" in dec.metadata, "entry_floor_down key missing from metadata even when None"
    assert dec.metadata["entry_floor_down"] is None


# ── LGB combo variant (v9_2_v12_combo_pure) ──────────────────────────────────
# In this file fill_price = YES/UP leg; NO proxy = 1 - fill_price.
# fill_price=0.35 → NO proxy=0.65.


def test_entry_floor_down_lgb_combo_blocks_when_no_proxy_below_floor():
    """LGB combo: fill_price=0.35 (NO proxy=0.65), floor=0.70 → SKIP.

    This models the eth_pure LOW bucket: YES≈0.35, NO≈0.65 — deep contrarian,
    model says YES is unlikely but market still prices it at 35¢.
    """
    from strategies.configs import v9_2_v12_combo_pure as _mod

    _mod._consec_state.clear()
    surface = _lgb_combo_surface(p_v92=0.15, p_v12=0.15, fill_price=0.35)
    with _gp_active({
        "expected_asset": "ETH",
        "down_threshold": 0.20,
        "up_threshold": 0.80,
        "eval_offset_min": 60,
        "eval_offset_max": 240,
        "min_consecutive_pass_ticks": 1,
        "entry_cap_down": 1.0,    # permissive cap so only floor_down fires
        "entry_floor_down": 0.70,
        "entry_cap": 0.95,
        "collateral_pct": 0.025,
        "gtc_cap": 0.96,
    }):
        dec = evaluate_v9_2_v12_combo_pure(surface)
    assert dec.action == "SKIP", f"expected SKIP, got {dec.action} (reason: {dec.skip_reason})"
    assert "entry_floor_down" in dec.skip_reason, dec.skip_reason


def test_entry_floor_down_lgb_combo_allows_when_no_proxy_above_floor():
    """LGB combo: fill_price=0.25 (NO proxy=0.75), floor=0.70 → TRADE.

    YES=0.25 means NO=0.75 — well above the 0.70 floor, fire allowed.
    """
    from strategies.configs import v9_2_v12_combo_pure as _mod

    _mod._consec_state.clear()
    surface = _lgb_combo_surface(p_v92=0.15, p_v12=0.15, fill_price=0.25)
    with _gp_active({
        "expected_asset": "ETH",
        "down_threshold": 0.20,
        "up_threshold": 0.80,
        "eval_offset_min": 60,
        "eval_offset_max": 240,
        "min_consecutive_pass_ticks": 1,
        "entry_cap_down": 1.0,
        "entry_floor_down": 0.70,
        "entry_cap": 0.95,
        "collateral_pct": 0.025,
        "gtc_cap": 0.96,
    }):
        dec = evaluate_v9_2_v12_combo_pure(surface)
    assert dec.action == "TRADE", f"expected TRADE, got {dec.action} (reason: {dec.skip_reason})"
    assert dec.direction == "DOWN"


def test_entry_floor_down_lgb_combo_none_default_no_skip():
    """LGB combo: entry_floor_down absent → no skip (back-compat)."""
    from strategies.configs import v9_2_v12_combo_pure as _mod

    _mod._consec_state.clear()
    surface = _lgb_combo_surface(p_v92=0.15, p_v12=0.15, fill_price=0.35)
    with _gp_active({
        "expected_asset": "ETH",
        "down_threshold": 0.20,
        "up_threshold": 0.80,
        "eval_offset_min": 60,
        "eval_offset_max": 240,
        "min_consecutive_pass_ticks": 1,
        "entry_cap_down": 1.0,
        # no entry_floor_down
        "entry_cap": 0.95,
        "collateral_pct": 0.025,
        "gtc_cap": 0.96,
    }):
        dec = evaluate_v9_2_v12_combo_pure(surface)
    assert dec.action == "TRADE", f"expected TRADE (back-compat), got {dec.action} (reason: {dec.skip_reason})"
    assert dec.direction == "DOWN"
