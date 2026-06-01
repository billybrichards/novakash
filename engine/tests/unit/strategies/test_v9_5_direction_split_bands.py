"""Tests for per-direction eval-offset band support in v9.5 LGB hooks (plan #806).

Covers the waterfall introduced in feat/v9_5-direction-split-bands:
  eval_offset_min/max_{up,down}  →  eval_offset_min/max  →  YAML default

Test matrix (all applied to both v9_5_eth_pure_lgb and v9_5_xrp_pure_lgb):

  1.  Symmetric back-compat: no direction keys → both UP and DOWN use same band.
  2.  Only _max_down set → DOWN uses tighter max; UP still uses symmetric max.
  3.  Only _max_up set → UP uses tighter max; DOWN still uses symmetric max.
  4.  Both direction keys set → each direction uses its own max.
  5.  _min_ variant: only _min_down tightens DOWN floor; UP floor unchanged.
  6.  End-to-end UP/DOWN split: t outside DOWN band → SKIP DOWN, TRADE UP.
  7.  Widened DOWN band (_max_down > sym max) → DOWN fires where it otherwise would not.
  8.  Metadata carries eval_offset_min/max_resolved showing the applied band.
  9.  Skip reason contains direction name and band limits for log analysis.
 10.  Motivating scenario (XRP PURE, RDS note #806): UP band 90-180, DOWN band 30-90
     (DISJOINT). t=50s → TRADE DOWN, SKIP UP; t=120s → TRADE UP, SKIP DOWN.
 11.  Back-compat: existing bare eval_offset_min/max override — both directions use it.
 12.  _resolve_eval_band unit: returns sym values when no direction-specific key.
 13.  _resolve_eval_band unit: direction key wins over sym.

Plan note: feat/v9_5-direction-split-bands; RDS note #806.
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

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from strategies import gate_params as _gp  # noqa: E402
from strategies.configs import v9_5_eth_pure_lgb  # noqa: E402
from strategies.configs import v9_5_xrp_pure_lgb  # noqa: E402
from strategies.configs.v9_5_eth_pure_lgb import (  # noqa: E402
    evaluate_v9_5_eth_pure_lgb,
    _resolve_eval_band as _resolve_eval_band_eth,
)
from strategies.configs.v9_5_xrp_pure_lgb import (  # noqa: E402
    evaluate_v9_5_xrp_pure_lgb,
    _resolve_eval_band as _resolve_eval_band_xrp,
)


# ── Gate-param helpers ────────────────────────────────────────────────────────

@contextmanager
def _gp_active(params):
    token = _gp.set_active(params)
    try:
        yield
    finally:
        _gp.reset_active(token)


# ── Surface factories ─────────────────────────────────────────────────────────

# Shared base for FullDataSurface — just enough fields to construct the object
# without touching the DB.  We use SimpleNamespace for the attribute reads that
# the strategy hooks call via getattr(); for strategies that import FullDataSurface
# they accept the actual class, but the hooks only call getattr so a namespace works
# for the evaluate_* call path as long as we set all required surface fields.

_UNIQUE_TS_COUNTER = 0


def _eth_surface(
    prob: float = 0.93,
    *,
    eval_offset: int = 120,
    asset: str = "ETH",
    window_ts: int | None = None,
) -> SimpleNamespace:
    """Build a minimal FullDataSurface-compatible namespace for ETH PURE tests."""
    global _UNIQUE_TS_COUNTER
    _UNIQUE_TS_COUNTER += 1
    ns = SimpleNamespace(
        asset=asset,
        eval_offset=eval_offset,
        window_ts=window_ts if window_ts is not None else (1779000000 + _UNIQUE_TS_COUNTER),
        probability_lgb_v9_5_eth_pure=prob,
        fill_price=0.65,
        clob_implied_up=0.65,
    )
    return ns


def _xrp_surface(
    prob: float = 0.93,
    *,
    eval_offset: int = 120,
    asset: str = "XRP",
    window_ts: int | None = None,
) -> SimpleNamespace:
    """Build a minimal FullDataSurface-compatible namespace for XRP PURE tests."""
    global _UNIQUE_TS_COUNTER
    _UNIQUE_TS_COUNTER += 1
    ns = SimpleNamespace(
        asset=asset,
        eval_offset=eval_offset,
        window_ts=window_ts if window_ts is not None else (1779100000 + _UNIQUE_TS_COUNTER),
        probability_lgb_v9_5_xrp_pure=prob,
        fill_price=0.65,
        clob_implied_up=0.65,
    )
    return ns


# ── Fixture: reset consec state between tests ─────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_consec_state():
    v9_5_eth_pure_lgb._consec_state.clear()
    v9_5_xrp_pure_lgb._consec_state.clear()
    yield
    v9_5_eth_pure_lgb._consec_state.clear()
    v9_5_xrp_pure_lgb._consec_state.clear()


# ── Base params helpers ───────────────────────────────────────────────────────
# Both strategies share the same gate-param key names; only thresholds differ.

_ETH_BASE = {
    "up_threshold": 0.915,
    "down_threshold": 0.095,
    "eval_offset_min": 60,
    "eval_offset_max": 210,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "ETH",
}

_XRP_BASE = {
    "up_threshold": 0.92,
    "down_threshold": 0.06,
    "eval_offset_min": 60,
    "eval_offset_max": 180,
    "min_consecutive_pass_ticks": 1,   # override XRP default of 2 for cleaner tests
    "expected_asset": "XRP",
}


# ── Unit tests: _resolve_eval_band ────────────────────────────────────────────


class TestResolveEvalBandUnit:
    """Direct unit tests for the _resolve_eval_band helper in each module."""

    def test_eth_no_direction_key_returns_sym(self):
        """No direction-specific keys → sym_min/sym_max returned unchanged."""
        with _gp_active({"eval_offset_min": 60, "eval_offset_max": 210}):
            up_min, up_max = _resolve_eval_band_eth("UP", sym_min=60, sym_max=210)
            dn_min, dn_max = _resolve_eval_band_eth("DOWN", sym_min=60, sym_max=210)
        assert (up_min, up_max) == (60, 210)
        assert (dn_min, dn_max) == (60, 210)

    def test_xrp_no_direction_key_returns_sym(self):
        with _gp_active({"eval_offset_min": 60, "eval_offset_max": 180}):
            up_min, up_max = _resolve_eval_band_xrp("UP", sym_min=60, sym_max=180)
            dn_min, dn_max = _resolve_eval_band_xrp("DOWN", sym_min=60, sym_max=180)
        assert (up_min, up_max) == (60, 180)
        assert (dn_min, dn_max) == (60, 180)

    def test_direction_key_wins_over_sym_down(self):
        """eval_offset_max_down=90 overrides sym_max=180 for DOWN only; UP unchanged."""
        with _gp_active({
            "eval_offset_max": 180,
            "eval_offset_max_down": 90,
        }):
            up_min, up_max = _resolve_eval_band_eth("UP", sym_min=60, sym_max=180)
            dn_min, dn_max = _resolve_eval_band_eth("DOWN", sym_min=60, sym_max=180)
        assert (up_min, up_max) == (60, 180)
        assert (dn_min, dn_max) == (60, 90)

    def test_direction_key_wins_over_sym_up(self):
        """eval_offset_max_up=160 overrides UP only; DOWN stays at sym."""
        with _gp_active({
            "eval_offset_max": 180,
            "eval_offset_max_up": 160,
        }):
            up_min, up_max = _resolve_eval_band_eth("UP", sym_min=60, sym_max=180)
            dn_min, dn_max = _resolve_eval_band_eth("DOWN", sym_min=60, sym_max=180)
        assert up_max == 160
        assert dn_max == 180

    def test_both_direction_keys_set(self):
        """Both _up and _down keys → each direction uses its own value."""
        with _gp_active({
            "eval_offset_max_up": 180,
            "eval_offset_max_down": 90,
            "eval_offset_min_up": 90,
            "eval_offset_min_down": 30,
        }):
            up_min, up_max = _resolve_eval_band_xrp("UP", sym_min=60, sym_max=180)
            dn_min, dn_max = _resolve_eval_band_xrp("DOWN", sym_min=60, sym_max=180)
        assert (up_min, up_max) == (90, 180)
        assert (dn_min, dn_max) == (30, 90)


# ── 1. Symmetric back-compat: both directions use same band ──────────────────


class TestSymmetricBackCompat:
    """No direction-specific keys → behaviour byte-identical to pre-change."""

    def test_eth_up_and_down_both_use_symmetric_band(self):
        """eval_offset=120 inside sym band [60,210] → TRADE for both directions."""
        surf_up = _eth_surface(prob=0.95, eval_offset=120)
        surf_dn = _eth_surface(prob=0.05, eval_offset=120)
        with _gp_active(_ETH_BASE):
            dec_up = evaluate_v9_5_eth_pure_lgb(surf_up)
        with _gp_active(_ETH_BASE):
            dec_dn = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_up.direction == "UP"
        assert dec_dn.action == "TRADE", dec_dn.skip_reason
        assert dec_dn.direction == "DOWN"

    def test_xrp_up_and_down_both_use_symmetric_band(self):
        surf_up = _xrp_surface(prob=0.95, eval_offset=120)
        surf_dn = _xrp_surface(prob=0.03, eval_offset=120)
        with _gp_active(_XRP_BASE):
            dec_up = evaluate_v9_5_xrp_pure_lgb(surf_up)
        with _gp_active(_XRP_BASE):
            dec_dn = evaluate_v9_5_xrp_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_up.direction == "UP"
        assert dec_dn.action == "TRADE", dec_dn.skip_reason
        assert dec_dn.direction == "DOWN"

    def test_eth_tick_outside_sym_band_skips(self):
        """eval_offset=50 outside symmetric band [60,210] → SKIP (both directions)."""
        surf_up = _eth_surface(prob=0.95, eval_offset=50)
        surf_dn = _eth_surface(prob=0.05, eval_offset=50)
        with _gp_active(_ETH_BASE):
            dec_up = evaluate_v9_5_eth_pure_lgb(surf_up)
        with _gp_active(_ETH_BASE):
            dec_dn = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec_up.action == "SKIP"
        assert dec_dn.action == "SKIP"


# ── 2. Only _max_down tightens DOWN; UP uses symmetric ───────────────────────


class TestOnlyMaxDownSet:
    """eval_offset_max_down tightens DOWN ceiling; UP ceiling stays at sym."""

    def test_eth_t_150_outside_down_band_but_inside_up_band(self):
        """t=150s: DOWN band max=120 → SKIP DOWN; UP band max=210 → TRADE UP."""
        surf_up = _eth_surface(prob=0.95, eval_offset=150)
        surf_dn = _eth_surface(prob=0.05, eval_offset=150)
        params = dict(_ETH_BASE, eval_offset_max_down=120)
        with _gp_active(params):
            dec_up = evaluate_v9_5_eth_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_up.direction == "UP"
        assert dec_dn.action == "SKIP"
        assert "outside_eval_band_DOWN" in dec_dn.skip_reason, dec_dn.skip_reason

    def test_xrp_t_150_outside_down_band_but_inside_up_band(self):
        """Same scenario on XRP PURE."""
        surf_up = _xrp_surface(prob=0.95, eval_offset=150)
        surf_dn = _xrp_surface(prob=0.03, eval_offset=150)
        params = dict(_XRP_BASE, eval_offset_max_down=90)
        with _gp_active(params):
            dec_up = evaluate_v9_5_xrp_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_xrp_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_up.direction == "UP"
        assert dec_dn.action == "SKIP"
        assert "outside_eval_band_DOWN" in dec_dn.skip_reason, dec_dn.skip_reason

    def test_down_in_tighter_band_still_trades(self):
        """t=80s is inside DOWN band 60-120 → TRADE DOWN."""
        surf_dn = _eth_surface(prob=0.05, eval_offset=80)
        params = dict(_ETH_BASE, eval_offset_max_down=120)
        with _gp_active(params):
            dec = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec.action == "TRADE", dec.skip_reason
        assert dec.direction == "DOWN"


# ── 3. Only _max_up set; DOWN uses symmetric ─────────────────────────────────


class TestOnlyMaxUpSet:
    """eval_offset_max_up tightens UP ceiling; DOWN ceiling stays at sym."""

    def test_eth_t_180_outside_up_band_but_inside_down_band(self):
        """t=180s: UP band max=150 → SKIP UP; DOWN band max=210 → TRADE DOWN."""
        surf_up = _eth_surface(prob=0.95, eval_offset=180)
        surf_dn = _eth_surface(prob=0.05, eval_offset=180)
        params = dict(_ETH_BASE, eval_offset_max_up=150)
        with _gp_active(params):
            dec_up = evaluate_v9_5_eth_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec_up.action == "SKIP"
        assert "outside_eval_band_UP" in dec_up.skip_reason, dec_up.skip_reason
        assert dec_dn.action == "TRADE", dec_dn.skip_reason
        assert dec_dn.direction == "DOWN"


# ── 4. Both direction keys set — each direction independent ──────────────────


class TestBothDirectionKeysSet:
    """eval_offset_max_up AND eval_offset_max_down each apply to their own direction."""

    def test_eth_split_bands_each_applies_to_own_direction(self):
        """UP max=180, DOWN max=120. t=150s: UP in band → TRADE; DOWN out → SKIP."""
        surf_up = _eth_surface(prob=0.95, eval_offset=150)
        surf_dn = _eth_surface(prob=0.05, eval_offset=150)
        params = dict(
            _ETH_BASE,
            eval_offset_max_up=180,
            eval_offset_max_down=120,
        )
        with _gp_active(params):
            dec_up = evaluate_v9_5_eth_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_dn.action == "SKIP"
        assert "120" in dec_dn.skip_reason, dec_dn.skip_reason

    def test_xrp_split_bands_both_keys_applied(self):
        """XRP PURE: UP max=180, DOWN max=90. t=120s: UP TRADE, DOWN SKIP."""
        surf_up = _xrp_surface(prob=0.95, eval_offset=120)
        surf_dn = _xrp_surface(prob=0.03, eval_offset=120)
        params = dict(
            _XRP_BASE,
            eval_offset_max_up=180,
            eval_offset_max_down=90,
        )
        with _gp_active(params):
            dec_up = evaluate_v9_5_xrp_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_xrp_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_dn.action == "SKIP"
        assert "90" in dec_dn.skip_reason, dec_dn.skip_reason


# ── 5. _min_ variant: only _min_down raises DOWN floor ───────────────────────


class TestOnlyMinDownSet:
    """eval_offset_min_down raises DOWN floor; UP floor stays at sym."""

    def test_eth_t_30_outside_down_floor_inside_up_floor(self):
        """t=30s: UP floor=60 → SKIP UP; DOWN floor=30 but UP floor still 60."""
        # Wait — UP floor is 60 so UP at t=30 should also skip under sym band.
        # Let's use t=45: UP floor=60 → SKIP UP, DOWN floor=30 → should TRADE DOWN.
        surf_up = _eth_surface(prob=0.95, eval_offset=45)
        surf_dn = _eth_surface(prob=0.05, eval_offset=45)
        params = dict(_ETH_BASE, eval_offset_min_down=30)
        with _gp_active(params):
            dec_up = evaluate_v9_5_eth_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec_up.action == "SKIP"  # UP floor still 60 → 45 < 60 → SKIP
        assert "UP" in dec_up.skip_reason
        assert dec_dn.action == "TRADE", dec_dn.skip_reason  # DOWN floor 30 → 45 ≥ 30
        assert dec_dn.direction == "DOWN"

    def test_eth_down_floor_raised_prevents_early_down(self):
        """eval_offset_min_down=50 raises DOWN floor: t=40 → SKIP DOWN."""
        surf_dn = _eth_surface(prob=0.05, eval_offset=40)
        params = dict(_ETH_BASE, eval_offset_min_down=50)
        with _gp_active(params):
            dec = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec.action == "SKIP"
        assert "DOWN" in dec.skip_reason, dec.skip_reason

    def test_eth_up_floor_unchanged_when_only_min_down_set(self):
        """UP floor stays at sym=60 when only eval_offset_min_down is set."""
        surf_up = _eth_surface(prob=0.95, eval_offset=40)
        params = dict(_ETH_BASE, eval_offset_min_down=30)
        with _gp_active(params):
            dec = evaluate_v9_5_eth_pure_lgb(surf_up)
        # t=40 < UP floor=60 → SKIP UP
        assert dec.action == "SKIP"
        assert "UP" in dec.skip_reason


# ── 6. End-to-end split: TRADE UP, SKIP DOWN at same offset ──────────────────


class TestEndToEndDirectionSplit:
    """Core integration test: same eval_offset fires UP but not DOWN when bands split."""

    def test_eth_t_160_up_band_wide_down_band_tight(self):
        """UP band [60,210], DOWN band [60,120]. t=160 → TRADE UP, SKIP DOWN."""
        surf_up = _eth_surface(prob=0.95, eval_offset=160)
        surf_dn = _eth_surface(prob=0.05, eval_offset=160)
        params = dict(_ETH_BASE, eval_offset_max_down=120)
        with _gp_active(params):
            dec_up = evaluate_v9_5_eth_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_up.direction == "UP"
        assert dec_dn.action == "SKIP"
        assert "outside_eval_band_DOWN" in dec_dn.skip_reason

    def test_xrp_t_160_up_band_wide_down_band_tight(self):
        """XRP PURE version of the same scenario."""
        surf_up = _xrp_surface(prob=0.95, eval_offset=160)
        surf_dn = _xrp_surface(prob=0.03, eval_offset=160)
        params = dict(_XRP_BASE, eval_offset_max_down=120)
        with _gp_active(params):
            dec_up = evaluate_v9_5_xrp_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_xrp_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_up.direction == "UP"
        assert dec_dn.action == "SKIP"
        assert "outside_eval_band_DOWN" in dec_dn.skip_reason


# ── 7. Widened DOWN band fires where it otherwise would not ──────────────────


class TestWidenedDownBand:
    """eval_offset_max_down wider than sym → DOWN fires at t that sym would block."""

    def test_eth_down_fires_at_t_220_with_widened_down_band(self):
        """sym max=210, DOWN max=240. t=220: DOWN in widened band → TRADE DOWN."""
        surf_dn = _eth_surface(prob=0.05, eval_offset=220)
        params = dict(_ETH_BASE, eval_offset_max_down=240)
        with _gp_active(params):
            dec = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec.action == "TRADE", dec.skip_reason
        assert dec.direction == "DOWN"

    def test_eth_up_still_blocked_at_t_220_with_sym_max_210(self):
        """At t=220, UP still uses sym max=210 → SKIP UP (no direction-specific UP key)."""
        surf_up = _eth_surface(prob=0.95, eval_offset=220)
        params = dict(_ETH_BASE, eval_offset_max_down=240)  # only DOWN widened
        with _gp_active(params):
            dec = evaluate_v9_5_eth_pure_lgb(surf_up)
        assert dec.action == "SKIP"
        assert "UP" in dec.skip_reason


# ── 8. Metadata carries resolved band ────────────────────────────────────────


class TestMetadataResolvedBand:
    """eval_offset_min/max_resolved in metadata shows the applied (direction-specific) band."""

    def test_eth_metadata_shows_down_specific_max(self):
        """When eval_offset_max_down=120 is applied, metadata shows max_resolved=120 for DOWN."""
        surf_dn = _eth_surface(prob=0.05, eval_offset=80)
        params = dict(_ETH_BASE, eval_offset_max_down=120)
        with _gp_active(params):
            dec = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec.action == "TRADE", dec.skip_reason
        assert dec.metadata.get("eval_offset_max_resolved") == 120

    def test_eth_metadata_shows_sym_max_for_up_when_only_down_key_set(self):
        """When only DOWN key is set, UP uses sym band → max_resolved = sym."""
        surf_up = _eth_surface(prob=0.95, eval_offset=80)
        params = dict(_ETH_BASE, eval_offset_max_down=120)
        with _gp_active(params):
            dec = evaluate_v9_5_eth_pure_lgb(surf_up)
        assert dec.action == "TRADE", dec.skip_reason
        assert dec.metadata.get("eval_offset_max_resolved") == 210  # ETH sym default

    def test_xrp_metadata_shows_direction_min_resolved(self):
        """eval_offset_min_up=90: UP min resolved shows 90; DOWN min shows sym=60."""
        surf_up = _xrp_surface(prob=0.95, eval_offset=120)
        surf_dn = _xrp_surface(prob=0.03, eval_offset=120)
        params = dict(_XRP_BASE, eval_offset_min_up=90)
        with _gp_active(params):
            dec_up = evaluate_v9_5_xrp_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_xrp_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_up.metadata.get("eval_offset_min_resolved") == 90
        assert dec_dn.action == "TRADE", dec_dn.skip_reason
        assert dec_dn.metadata.get("eval_offset_min_resolved") == 60  # sym fallback


# ── 9. Skip reason contains direction and band limits ────────────────────────


class TestSkipReasonGranularity:
    """Skip reason encodes direction and band limits for log analysis."""

    def test_skip_reason_contains_down_and_limit_value(self):
        """SKIP for DOWN contains 'DOWN' and the DOWN max value."""
        surf_dn = _eth_surface(prob=0.05, eval_offset=150)
        params = dict(_ETH_BASE, eval_offset_max_down=120)
        with _gp_active(params):
            dec = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec.action == "SKIP"
        assert "DOWN" in dec.skip_reason
        assert "120" in dec.skip_reason

    def test_skip_reason_contains_up_and_limit_value(self):
        """SKIP for UP contains 'UP' and the UP max value."""
        surf_up = _eth_surface(prob=0.95, eval_offset=160)
        params = dict(_ETH_BASE, eval_offset_max_up=130)
        with _gp_active(params):
            dec = evaluate_v9_5_eth_pure_lgb(surf_up)
        assert dec.action == "SKIP"
        assert "UP" in dec.skip_reason
        assert "130" in dec.skip_reason

    def test_xrp_skip_reason_contains_direction_and_limit(self):
        surf_dn = _xrp_surface(prob=0.03, eval_offset=120)
        params = dict(_XRP_BASE, eval_offset_max_down=90)
        with _gp_active(params):
            dec = evaluate_v9_5_xrp_pure_lgb(surf_dn)
        assert dec.action == "SKIP"
        assert "DOWN" in dec.skip_reason
        assert "90" in dec.skip_reason


# ── 10. Motivating scenario: RDS note #806 disjoint XRP PURE bands ───────────


class TestPlan806MotivatingScenario:
    """RDS note #806: XRP PURE UP optimal 90-180, DOWN optimal 30-90 — disjoint.

    t=50s: in DOWN band 30-90, outside UP band 90-180 → TRADE DOWN, SKIP UP.
    t=120s: in UP band 90-180, outside DOWN band 30-90 → TRADE UP, SKIP DOWN.
    """

    _PARAMS = {
        "up_threshold": 0.92,
        "down_threshold": 0.06,
        "eval_offset_min": 60,       # symmetric fallback (unused when direction keys set)
        "eval_offset_max": 180,
        "eval_offset_min_up": 90,
        "eval_offset_max_up": 180,
        "eval_offset_min_down": 30,
        "eval_offset_max_down": 90,
        "min_consecutive_pass_ticks": 1,
        "expected_asset": "XRP",
    }

    def test_t50_trades_down_skips_up(self):
        """t=50s is inside DOWN band [30,90] but outside UP band [90,180]."""
        surf_up = _xrp_surface(prob=0.95, eval_offset=50)
        surf_dn = _xrp_surface(prob=0.03, eval_offset=50)
        with _gp_active(self._PARAMS):
            dec_up = evaluate_v9_5_xrp_pure_lgb(surf_up)
        with _gp_active(self._PARAMS):
            dec_dn = evaluate_v9_5_xrp_pure_lgb(surf_dn)
        assert dec_up.action == "SKIP", f"expected SKIP UP at t=50, got {dec_up.action}"
        assert "UP" in dec_up.skip_reason
        assert dec_dn.action == "TRADE", f"expected TRADE DOWN at t=50, got: {dec_dn.skip_reason}"
        assert dec_dn.direction == "DOWN"

    def test_t120_trades_up_skips_down(self):
        """t=120s is inside UP band [90,180] but outside DOWN band [30,90]."""
        surf_up = _xrp_surface(prob=0.95, eval_offset=120)
        surf_dn = _xrp_surface(prob=0.03, eval_offset=120)
        with _gp_active(self._PARAMS):
            dec_up = evaluate_v9_5_xrp_pure_lgb(surf_up)
        with _gp_active(self._PARAMS):
            dec_dn = evaluate_v9_5_xrp_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", f"expected TRADE UP at t=120, got: {dec_up.skip_reason}"
        assert dec_up.direction == "UP"
        assert dec_dn.action == "SKIP", f"expected SKIP DOWN at t=120, got {dec_dn.action}"
        assert "DOWN" in dec_dn.skip_reason

    def test_metadata_shows_correct_bands_at_t50(self):
        """At t=50 DOWN trade: metadata shows DOWN-specific min=30, max=90."""
        surf_dn = _xrp_surface(prob=0.03, eval_offset=50)
        with _gp_active(self._PARAMS):
            dec = evaluate_v9_5_xrp_pure_lgb(surf_dn)
        assert dec.action == "TRADE", dec.skip_reason
        assert dec.metadata.get("eval_offset_min_resolved") == 30
        assert dec.metadata.get("eval_offset_max_resolved") == 90

    def test_metadata_shows_correct_bands_at_t120(self):
        """At t=120 UP trade: metadata shows UP-specific min=90, max=180."""
        surf_up = _xrp_surface(prob=0.95, eval_offset=120)
        with _gp_active(self._PARAMS):
            dec = evaluate_v9_5_xrp_pure_lgb(surf_up)
        assert dec.action == "TRADE", dec.skip_reason
        assert dec.metadata.get("eval_offset_min_resolved") == 90
        assert dec.metadata.get("eval_offset_max_resolved") == 180


# ── 11. Back-compat: bare symmetric override keeps working ───────────────────


class TestBackCompatSymmetricOverride:
    """Existing bare eval_offset_min/max runtime override rows are unaffected."""

    def test_eth_bare_runtime_override_applies_to_both_directions(self):
        """eval_offset_max=200 (bare) without direction keys → both UP/DOWN use 200."""
        surf_up = _eth_surface(prob=0.95, eval_offset=195)
        surf_dn = _eth_surface(prob=0.05, eval_offset=195)
        params = {
            "up_threshold": 0.915,
            "down_threshold": 0.095,
            "eval_offset_min": 60,
            "eval_offset_max": 200,   # widened bare override
            "min_consecutive_pass_ticks": 1,
            "expected_asset": "ETH",
        }
        with _gp_active(params):
            dec_up = evaluate_v9_5_eth_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_up.metadata.get("eval_offset_max_resolved") == 200
        assert dec_dn.action == "TRADE", dec_dn.skip_reason
        assert dec_dn.metadata.get("eval_offset_max_resolved") == 200

    def test_xrp_bare_runtime_override_applies_to_both_directions(self):
        """Same on XRP PURE — bare eval_offset_max=200 applies to both UP and DOWN."""
        surf_up = _xrp_surface(prob=0.95, eval_offset=190)
        surf_dn = _xrp_surface(prob=0.03, eval_offset=190)
        params = {
            "up_threshold": 0.92,
            "down_threshold": 0.06,
            "eval_offset_min": 60,
            "eval_offset_max": 200,
            "min_consecutive_pass_ticks": 1,
            "expected_asset": "XRP",
        }
        with _gp_active(params):
            dec_up = evaluate_v9_5_xrp_pure_lgb(surf_up)
        with _gp_active(params):
            dec_dn = evaluate_v9_5_xrp_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_dn.action == "TRADE", dec_dn.skip_reason
        assert dec_up.metadata.get("eval_offset_max_resolved") == 200
        assert dec_dn.metadata.get("eval_offset_max_resolved") == 200

    def test_eth_no_override_uses_yaml_defaults(self):
        """No runtime override at all → YAML defaults [60,210] applied to both."""
        surf_up = _eth_surface(prob=0.95, eval_offset=120)
        surf_dn = _eth_surface(prob=0.05, eval_offset=120)
        with _gp_active({}):
            dec_up = evaluate_v9_5_eth_pure_lgb(surf_up)
        with _gp_active({}):
            dec_dn = evaluate_v9_5_eth_pure_lgb(surf_dn)
        assert dec_up.action == "TRADE", dec_up.skip_reason
        assert dec_dn.action == "TRADE", dec_dn.skip_reason
        assert dec_up.metadata.get("eval_offset_max_resolved") == 210
        assert dec_dn.metadata.get("eval_offset_max_resolved") == 210
