"""Tests for direction-specific v4_regime block gate.

Covers _block_down_v4_regimes / _block_up_v4_regimes helpers and the
``v4_regime_direction`` gate in both v8_champion_lgb_only and v9_ensemble.

Hub note #347 motivation:
  - YES × volatile_trend  83% WR  +$33  (keep)
  - NO  × volatile_trend  68% WR  -$87  (block)
  - NO  × risk_off        82% WR  +$34  (keep)
  - YES × risk_off        74% WR   +$1  (marginal)

Gate behaviour:
  - Default (empty lists) = no-op; all prior tests remain green.
  - DOWN blocked when v4_regime in block_down_v4_regimes.
  - UP   blocked when v4_regime in block_up_v4_regimes.
  - Asymmetric: blocking DOWN does NOT affect UP (and vice versa).
  - skip_reason contains "v4_regime_direction".
  - Gate name in gate_results is "v4_regime_direction".
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v8_champion_lgb_only import (
    evaluate_v8_champion_lgb_only,
    reset_cooldown,
    reset_all_confirmations,
)
from strategies.configs.v9_ensemble import (
    evaluate_v9_ensemble,
    reset_cooldown as reset_cooldown_v9,
    reset_all_confirmations_v9,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface

# ── Surface factory helpers ────────────────────────────────────────────────
# Shared with test_v8_champion_lgb_only pattern — DOWN default, UP override.

def _make_surface(**overrides) -> FullDataSurface:
    """Default valid DOWN surface — all gates pass, v4_regime=volatile_trend."""
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.30, v2_probability_raw=0.30,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.30, probability_classifier=None,
        ensemble_config={"mode": "blend"},
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="volatile_trend", v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BEAR", v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.30,
        poly_confidence_distance=0.20, poly_timing="optimal",
        poly_max_entry_price=0.60, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.38, clob_up_ask=0.40, clob_down_bid=0.53,
        clob_down_ask=0.55, clob_implied_up=0.40,
        gamma_up_price=0.40, gamma_down_price=0.60,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _up_surface(**overrides) -> FullDataSurface:
    """Symmetric UP-direction surface."""
    defaults = dict(
        poly_direction="UP", poly_confidence=0.70,
        poly_confidence_distance=0.20,
        probability_lgb=0.70, probability_classifier=None,
        delta_binance=+0.005, delta_tiingo=+0.004, delta_chainlink=+0.005,
        v4_recommended_side="UP", v2_probability_up=0.70,
        clob_up_bid=0.58, clob_up_ask=0.60,
        clob_down_bid=0.38, clob_down_ask=0.40,
        gamma_up_price=0.60, gamma_down_price=0.40,
    )
    defaults.update(overrides)
    return _make_surface(**defaults)


# ── Gate-params base sets ──────────────────────────────────────────────────

_BASE_V8 = dict(
    min_offset_sec=30,
    max_offset_sec=200,
    tradeable_v4_regimes=["volatile_trend", "chop", "risk_off", "calm_trend"],
    block_down_vpin_regimes=["TRANSITION"],
    block_up_vpin_regimes=[],
    # NEW keys under test — empty by default
    block_down_v4_regimes=[],
    block_up_v4_regimes=[],
    use_classifier_bucket=False,
    lgb_dist_min_down=0.10,
    lgb_dist_min_up=0.15,
    fill_band_min=0.00,
    fill_band_max=0.82,
    up_min_fill_price=0.55,
    up_require_both_buckets=False,
    down_min_fill_price=0.15,
    blocked_utc_hours=[0, 1, 2, 3, 4, 5],
    source_agreement_require_chainlink=True,
    source_agreement_require_tiingo=True,
    skip_on_oracle_disagree=True,
    vpin_min=0.40,
    vpin_max=1.0,
    post_loss_cooldown_min=20,
    transition_strong_bypass_enabled=False,   # keep simple for these tests
    transition_bypass_min_avg_pct_delta=0.05,
    transition_bypass_min_lgb_dist=0.20,
    delta_gate_enabled=False,
    min_consecutive_pass_ticks=0,
)

_BASE_V9 = dict(
    _BASE_V8,
    ensemble_disagreement_threshold=0.25,
    require_direction_agreement=True,
    lgb_dist_min_down_with_hc_agree=0.05,
    lgb_dist_min_up_with_hc_agree=0.05,
    vhc_threshold=0.25,
    vhc_bypass_transition=True,
    vhc_bypass_up_dist=True,
    vhc_bypass_disagreement=True,
    vhc_bypass_lgb_safety_floor=True,
    vhc_bypass_oracle_direction=True,
    vhc_kelly_multiplier=2.0,
    pl_vhc_bypass_enabled=False,   # keep simple
    pl_vhc_threshold=0.25,
    pl_vhc_require_pc_agreement=True,
    conviction_high_dist=0.20,
    conviction_medium_dist=0.12,
    conviction_low_dist=0.05,
    fallback_to_lgb_on_pc_null=True,
    pc_weight_t_60=0.55,
    pc_weight_t_120=0.50,
    pc_weight_t_180=0.45,
    pc_weight_t_200=0.35,
    up_max_fill_price=1.0,
    down_max_fill_price=1.0,
)


# ── v8_champion_lgb_only tests ─────────────────────────────────────────────

class TestV8V4RegimeDirectionBlock:
    """Direction-specific v4_regime block in v8_champion_lgb_only."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        reset_cooldown()
        reset_all_confirmations()
        yield
        reset_cooldown()
        reset_all_confirmations()

    def test_empty_lists_no_op_down(self):
        """Default empty lists must not block anything (regression safety)."""
        token = _gp.set_active(dict(_BASE_V8, block_down_v4_regimes=[], block_up_v4_regimes=[]))
        try:
            d = evaluate_v8_champion_lgb_only(_make_surface(v4_regime="volatile_trend"))
            assert d.action == "TRADE", d.skip_reason
            assert d.direction == "DOWN"
        finally:
            _gp.reset_active(token)

    def test_empty_lists_no_op_up(self):
        """Default empty lists must not block UP either."""
        token = _gp.set_active(dict(_BASE_V8, block_down_v4_regimes=[], block_up_v4_regimes=[]))
        try:
            d = evaluate_v8_champion_lgb_only(_up_surface(v4_regime="volatile_trend"))
            assert d.action == "TRADE", d.skip_reason
            assert d.direction == "UP"
        finally:
            _gp.reset_active(token)

    def test_down_blocked_when_v4_regime_in_block_down(self):
        """DOWN must be blocked when v4_regime is in block_down_v4_regimes."""
        token = _gp.set_active(dict(
            _BASE_V8,
            block_down_v4_regimes=["volatile_trend"],
            block_up_v4_regimes=[],
        ))
        try:
            d = evaluate_v8_champion_lgb_only(_make_surface(v4_regime="volatile_trend"))
            assert d.action == "SKIP"
            assert "v4_regime_direction" in d.skip_reason
        finally:
            _gp.reset_active(token)

    def test_up_unaffected_when_only_down_blocked(self):
        """Blocking DOWN in volatile_trend must NOT affect UP direction."""
        token = _gp.set_active(dict(
            _BASE_V8,
            block_down_v4_regimes=["volatile_trend"],
            block_up_v4_regimes=[],
        ))
        try:
            d = evaluate_v8_champion_lgb_only(_up_surface(v4_regime="volatile_trend"))
            assert d.action == "TRADE", d.skip_reason
            assert d.direction == "UP"
        finally:
            _gp.reset_active(token)

    def test_up_blocked_when_v4_regime_in_block_up(self):
        """UP must be blocked when v4_regime is in block_up_v4_regimes."""
        token = _gp.set_active(dict(
            _BASE_V8,
            block_down_v4_regimes=[],
            block_up_v4_regimes=["volatile_trend"],
        ))
        try:
            d = evaluate_v8_champion_lgb_only(_up_surface(v4_regime="volatile_trend"))
            assert d.action == "SKIP"
            assert "v4_regime_direction" in d.skip_reason
        finally:
            _gp.reset_active(token)

    def test_down_unaffected_when_only_up_blocked(self):
        """Blocking UP must NOT affect DOWN direction."""
        token = _gp.set_active(dict(
            _BASE_V8,
            block_down_v4_regimes=[],
            block_up_v4_regimes=["volatile_trend"],
        ))
        try:
            d = evaluate_v8_champion_lgb_only(_make_surface(v4_regime="volatile_trend"))
            assert d.action == "TRADE", d.skip_reason
            assert d.direction == "DOWN"
        finally:
            _gp.reset_active(token)

    def test_unblocked_regime_still_trades(self):
        """DOWN in chop should trade when only volatile_trend is blocked for DOWN."""
        token = _gp.set_active(dict(
            _BASE_V8,
            block_down_v4_regimes=["volatile_trend"],
            block_up_v4_regimes=[],
        ))
        try:
            d = evaluate_v8_champion_lgb_only(_make_surface(v4_regime="chop"))
            assert d.action == "TRADE", d.skip_reason
            assert d.direction == "DOWN"
        finally:
            _gp.reset_active(token)

    def test_both_directions_blocked_in_same_regime(self):
        """Both directions blocked in volatile_trend: DOWN and UP both skip."""
        token = _gp.set_active(dict(
            _BASE_V8,
            block_down_v4_regimes=["volatile_trend"],
            block_up_v4_regimes=["volatile_trend"],
        ))
        try:
            d_down = evaluate_v8_champion_lgb_only(_make_surface(v4_regime="volatile_trend"))
            assert d_down.action == "SKIP"
            assert "v4_regime_direction" in d_down.skip_reason

            d_up = evaluate_v8_champion_lgb_only(_up_surface(v4_regime="volatile_trend"))
            assert d_up.action == "SKIP"
            assert "v4_regime_direction" in d_up.skip_reason
        finally:
            _gp.reset_active(token)

    def test_gate_result_in_metadata(self):
        """Failing gate must appear in gate_results metadata."""
        token = _gp.set_active(dict(
            _BASE_V8,
            block_down_v4_regimes=["volatile_trend"],
            block_up_v4_regimes=[],
        ))
        try:
            d = evaluate_v8_champion_lgb_only(_make_surface(v4_regime="volatile_trend"))
            assert d.action == "SKIP"
            gate_names = [g["gate"] for g in (d.metadata or {}).get("gate_results", [])]
            assert "v4_regime_direction" in gate_names
            v4_gate = next(g for g in d.metadata["gate_results"] if g["gate"] == "v4_regime_direction")
            assert v4_gate["passed"] is False
        finally:
            _gp.reset_active(token)

    def test_passing_gate_in_metadata(self):
        """Passing gate must appear as passed=True in gate_results."""
        token = _gp.set_active(dict(
            _BASE_V8,
            block_down_v4_regimes=[],
            block_up_v4_regimes=[],
        ))
        try:
            d = evaluate_v8_champion_lgb_only(_make_surface(v4_regime="volatile_trend"))
            assert d.action == "TRADE", d.skip_reason
            gate_names = [g["gate"] for g in (d.metadata or {}).get("gate_results", [])]
            assert "v4_regime_direction" in gate_names
            v4_gate = next(g for g in d.metadata["gate_results"] if g["gate"] == "v4_regime_direction")
            assert v4_gate["passed"] is True
        finally:
            _gp.reset_active(token)

    def test_hub_note_347_scenario_block_no_volatile_trend(self):
        """Hub note #347: block NO×volatile_trend, keep YES×volatile_trend."""
        token = _gp.set_active(dict(
            _BASE_V8,
            block_down_v4_regimes=["volatile_trend"],
            block_up_v4_regimes=[],
        ))
        try:
            # NO (DOWN) in volatile_trend → should SKIP
            down = evaluate_v8_champion_lgb_only(_make_surface(v4_regime="volatile_trend"))
            assert down.action == "SKIP"
            assert "v4_regime_direction" in down.skip_reason

            # YES (UP) in volatile_trend → should TRADE
            up = evaluate_v8_champion_lgb_only(_up_surface(v4_regime="volatile_trend"))
            assert up.action == "TRADE", up.skip_reason
            assert up.direction == "UP"
        finally:
            _gp.reset_active(token)


# ── v9_ensemble tests ──────────────────────────────────────────────────────

class TestV9V4RegimeDirectionBlock:
    """Direction-specific v4_regime block in v9_ensemble (LGB-only fallback path)."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        reset_cooldown_v9()
        reset_all_confirmations_v9()
        yield
        reset_cooldown_v9()
        reset_all_confirmations_v9()

    def _v9_surface_down(self, **overrides) -> FullDataSurface:
        """pc=None so v9 falls back to v8_lgb_only path."""
        return _make_surface(probability_classifier=None, **overrides)

    def _v9_surface_up(self, **overrides) -> FullDataSurface:
        return _up_surface(probability_classifier=None, **overrides)

    def test_empty_lists_no_op_down(self):
        """Empty lists → no-op in v9 path."""
        token = _gp.set_active(dict(_BASE_V9, block_down_v4_regimes=[], block_up_v4_regimes=[]))
        try:
            d = evaluate_v9_ensemble(self._v9_surface_down(v4_regime="volatile_trend"))
            assert d.action == "TRADE", d.skip_reason
        finally:
            _gp.reset_active(token)

    def test_empty_lists_no_op_up(self):
        token = _gp.set_active(dict(_BASE_V9, block_down_v4_regimes=[], block_up_v4_regimes=[]))
        try:
            d = evaluate_v9_ensemble(self._v9_surface_up(v4_regime="volatile_trend"))
            assert d.action == "TRADE", d.skip_reason
        finally:
            _gp.reset_active(token)

    def test_down_blocked_in_volatile_trend(self):
        """v9: DOWN in volatile_trend blocked via block_down_v4_regimes."""
        token = _gp.set_active(dict(
            _BASE_V9,
            block_down_v4_regimes=["volatile_trend"],
            block_up_v4_regimes=[],
        ))
        try:
            d = evaluate_v9_ensemble(self._v9_surface_down(v4_regime="volatile_trend"))
            assert d.action == "SKIP"
            assert "v4_regime_direction" in d.skip_reason
        finally:
            _gp.reset_active(token)

    def test_up_unaffected_when_only_down_blocked(self):
        """v9: Blocking DOWN does not block UP."""
        token = _gp.set_active(dict(
            _BASE_V9,
            block_down_v4_regimes=["volatile_trend"],
            block_up_v4_regimes=[],
        ))
        try:
            d = evaluate_v9_ensemble(self._v9_surface_up(v4_regime="volatile_trend"))
            assert d.action == "TRADE", d.skip_reason
            assert d.direction == "UP"
        finally:
            _gp.reset_active(token)

    def test_up_blocked_in_risk_off(self):
        """v9: UP in risk_off blocked when configured."""
        token = _gp.set_active(dict(
            _BASE_V9,
            block_down_v4_regimes=[],
            block_up_v4_regimes=["risk_off"],
        ))
        try:
            d = evaluate_v9_ensemble(self._v9_surface_up(v4_regime="risk_off"))
            assert d.action == "SKIP"
            assert "v4_regime_direction" in d.skip_reason
        finally:
            _gp.reset_active(token)

    def test_down_unaffected_when_only_up_blocked(self):
        """v9: Blocking UP in risk_off does not block DOWN in risk_off."""
        token = _gp.set_active(dict(
            _BASE_V9,
            block_down_v4_regimes=[],
            block_up_v4_regimes=["risk_off"],
        ))
        try:
            d = evaluate_v9_ensemble(self._v9_surface_down(v4_regime="risk_off"))
            assert d.action == "TRADE", d.skip_reason
            assert d.direction == "DOWN"
        finally:
            _gp.reset_active(token)

    def test_hub_note_347_scenario_v9(self):
        """Hub note #347 on v9: block NO×volatile_trend, keep YES×volatile_trend."""
        token = _gp.set_active(dict(
            _BASE_V9,
            block_down_v4_regimes=["volatile_trend"],
            block_up_v4_regimes=[],
        ))
        try:
            down = evaluate_v9_ensemble(self._v9_surface_down(v4_regime="volatile_trend"))
            assert down.action == "SKIP"
            assert "v4_regime_direction" in down.skip_reason

            up = evaluate_v9_ensemble(self._v9_surface_up(v4_regime="volatile_trend"))
            assert up.action == "TRADE", up.skip_reason
            assert up.direction == "UP"
        finally:
            _gp.reset_active(token)
