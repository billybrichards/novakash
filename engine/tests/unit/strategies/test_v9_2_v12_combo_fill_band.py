"""Unit tests for v9_2_v12_combo direction-aware fill-band gate (RDS note #664).

Focused tests for the entry_floor_up / entry_cap_down gate added 2026-05-25.
These tests verify:
- UP fire at fill < 0.60 → SKIP (fill_below_up_floor)
- UP fire at fill = 0.60 → TRADE (boundary inclusive)
- DOWN fire at fill >= 0.90 → SKIP (fill_above_down_cap)
- DOWN fire at fill = 0.89 → TRADE (boundary inclusive on allow side)
- UP at fill >= 0.90 still TRADES (direction-aware: 100% WR zone 5W/0L)
- DOWN at fill < 0.60 still TRADES (direction-aware: 100% WR zone 3W/0L)
- No fill price (clob_implied_up=None) → gate bypassed
- Default permissive params (no entry_floor_up / entry_cap_down in YAML) → all fills pass

Why direction-aware not symmetric: per today's gamma data (RDS notes #661 + #664),
a symmetric [0.60, 0.90] band would BLOCK profitable zones (UP@>=0.90 = 5W/0L,
DOWN@<0.60 = 3W/0L). The fix is two separate one-sided gates.
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_2_v12_combo import evaluate_v9_2_v12_combo
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    """Default surface: BTC, eval_offset=120, both v9_2 + v12 above UP thresholds,
    clob_implied_up=0.65 (well inside the [0.60, 0.90) safe zone)."""
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=1713009600,
        eval_offset=120, assembled_at=time.time(),
        current_price=110000.0, open_price=109500.0,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.80, v2_probability_raw=0.80,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.80, probability_classifier=None,
        ensemble_config={"mode": "blend"},
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="volatile_trend", v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BULL", v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="UP", poly_trade_advised=True, poly_confidence=0.70,
        poly_confidence_distance=0.20, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="UP", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.63, clob_up_ask=0.65, clob_down_bid=0.28,
        clob_down_ask=0.30, clob_implied_up=0.65,
        gamma_up_price=0.65, gamma_down_price=0.35,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=180,
        probability_lgb_v9_2=0.80,
        probability_lgb_v12=0.75,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# Base params that include the new fill-band gate fields.
_BASE_PARAMS: dict[str, Any] = {
    "up_v92_threshold": 0.75,
    "up_v12_threshold": 0.70,
    "down_v92_threshold": 0.20,
    "down_v12_threshold": 0.40,
    "eval_offset_min": 60,
    "eval_offset_max": 210,
    "min_consecutive_pass_ticks": 1,
    "entry_cap": 0.85,
    "entry_floor_up": 0.60,
    "entry_cap_down": 0.90,
    "collateral_pct": 0.025,
    "gtc_cap": 0.90,
}


@contextmanager
def _params(**extra):
    p = dict(_BASE_PARAMS)
    p.update(extra)
    token = _gp.set_active(p)
    try:
        yield
    finally:
        _gp.reset_active(token)


@pytest.fixture(autouse=True)
def _clear_consec_state():
    from strategies.configs import v9_2_v12_combo
    v9_2_v12_combo._consec_state.clear()
    yield
    v9_2_v12_combo._consec_state.clear()


# ── DOWN surface helper ────────────────────────────────────────────────────

def _down_surface(**extra):
    """DOWN-direction surface: both v9_2 + v12 below DOWN thresholds."""
    return _make_surface(
        probability_lgb_v9_2=0.10,
        probability_lgb_v12=0.30,
        **extra,
    )


# ── UP gate (entry_floor_up=0.60) ─────────────────────────────────────────


class TestUpFillFloor:
    def test_up_fill_055_below_floor_skips(self):
        """UP fire at fill=0.55 < 0.60 — SKIP (25% WR zone per RDS #664)."""
        surface = _make_surface(clob_implied_up=0.55)
        with _params():
            d = evaluate_v9_2_v12_combo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_below_up_floor" in d.skip_reason
        assert "0.550" in d.skip_reason
        assert "0.600" in d.skip_reason

    def test_up_fill_at_060_exact_trades(self):
        """UP fire at fill=0.60 exactly — boundary inclusive, TRADE."""
        surface = _make_surface(clob_implied_up=0.60)
        with _params():
            d = evaluate_v9_2_v12_combo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_fill_065_in_safe_zone_trades(self):
        """UP fire at fill=0.65 — well inside safe zone, TRADE."""
        surface = _make_surface(clob_implied_up=0.65)
        with _params():
            d = evaluate_v9_2_v12_combo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_fill_092_above_090_still_trades(self):
        """UP at fill=0.92 must TRADE — 100% WR zone (5W/0L today).
        Symmetric gate would wrongly block this; direction-aware gate does NOT."""
        surface = _make_surface(clob_implied_up=0.92)
        with _params():
            d = evaluate_v9_2_v12_combo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"


# ── DOWN gate (entry_cap_down=0.90) ───────────────────────────────────────


class TestDownFillCap:
    def test_down_fill_at_090_skips(self):
        """DOWN fire at fill=0.90 (>= 0.90) — SKIP (33% WR per RDS #664)."""
        surface = _down_surface(clob_implied_up=0.90)
        with _params():
            d = evaluate_v9_2_v12_combo(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_above_down_cap" in d.skip_reason
        assert "0.900" in d.skip_reason

    def test_down_fill_above_090_skips(self):
        """DOWN fire at fill=0.92 — also above cap, SKIP."""
        surface = _down_surface(clob_implied_up=0.92)
        with _params():
            d = evaluate_v9_2_v12_combo(surface)
        assert d.action == "SKIP"
        assert "fill_above_down_cap" in (d.skip_reason or "")

    def test_down_fill_at_089_trades(self):
        """DOWN fire at fill=0.89 (< 0.90) — boundary inclusive on allow side, TRADE."""
        surface = _down_surface(clob_implied_up=0.89)
        with _params():
            d = evaluate_v9_2_v12_combo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fill_055_below_060_still_trades(self):
        """DOWN at fill=0.55 (below 0.60) must TRADE — 100% WR zone (3W/0L today).
        Symmetric gate would wrongly block this; direction-aware gate does NOT."""
        surface = _down_surface(clob_implied_up=0.55)
        with _params():
            d = evaluate_v9_2_v12_combo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"


# ── No fill price → gate bypassed ──────────────────────────────────────────


class TestNoFillPrice:
    def test_none_clob_implied_up_bypasses_gate_up(self):
        """When clob_implied_up is None, fill gate is skipped — UP fires normally."""
        surface = _make_surface(clob_implied_up=None)
        with _params():
            d = evaluate_v9_2_v12_combo(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_clob_implied_up_bypasses_gate_down(self):
        """When clob_implied_up is None, fill gate is skipped — DOWN fires normally."""
        surface = _down_surface(clob_implied_up=None)
        with _params():
            d = evaluate_v9_2_v12_combo(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"


# ── Default permissive (strats without new params are unaffected) ──────────


class TestDefaultPermissive:
    """Strategies NOT setting entry_floor_up / entry_cap_down default to
    entry_floor_up=0.0, entry_cap_down=1.0 — all fills pass."""

    def test_default_params_allow_very_low_up_fill(self):
        """Without entry_floor_up/entry_cap_down in params, fill=0.10 UP passes."""
        surface = _make_surface(clob_implied_up=0.10)
        # Params without the new gate fields.
        permissive = {k: v for k, v in _BASE_PARAMS.items()
                      if k not in ("entry_floor_up", "entry_cap_down")}
        token = _gp.set_active(permissive)
        try:
            d = evaluate_v9_2_v12_combo(surface)
        finally:
            _gp.reset_active(token)
        assert d.action == "TRADE"
        assert d.direction == "UP"
