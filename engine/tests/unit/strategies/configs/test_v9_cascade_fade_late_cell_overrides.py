"""Unit tests for v9_cascade_fade_late per-cell lgb_dist_min override (hub #402, PR #506).

Coverage:
  1. Cell override relaxes DOWN lgb_dist_min -> no conviction-floor SKIP
  2. Cell override tightens DOWN lgb_dist_min -> SKIP lgb_safety_floor
  3. No cell match -> falls back to strategy-level lgb_dist_min_down
  4. Empty param_overrides_by_cell -> no-op (strategy-level floor applies)
  5. Multiple cells in map; only exact match fires
  6. Runtime-tunability: override changes mid-session via gate_params contextvar
  7. UP cell override: relaxes lgb_dist_min_up when strategy-level also relaxed
  8. cell_param_overrides_active stamped in metadata when override matched
  9. No metadata key when no override matched
  10. probability_lgb unavailable -> SKIP before any cell override logic
"""
from __future__ import annotations

import os
import sys
import time

import pytest

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from strategies.data_surface import FullDataSurface
from strategies import gate_params as _gp
from strategies.gates.cell_param_overrides import build_cell_key
from strategies.configs.v9_ensemble import (
    reset_all_confirmations_v9,
    reset_cooldown,
)

# Window that resolves to us_pm (14-17 UTC) -- 2026-05-08 15:00:00 UTC
_TS_US_PM = 1746716400
_OFFSET_40 = 40  # in [24,60] window band, resolves to T-31-60
_REGIME = "CASCADE"
_P_DOWN = 0.25   # dist=0.25


def _make_surface(**overrides) -> FullDataSurface:
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=_TS_US_PM,
        eval_offset=_OFFSET_40,
        assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime=_REGIME, twap_delta=-0.003,
        v2_probability_up=0.30, v2_probability_raw=0.30,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=_P_DOWN,
        probability_classifier=None,
        ensemble_config=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None, v3_sub_momentum=None,
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
        hour_utc=15, seconds_to_close=_OFFSET_40,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _down_key(offset=_OFFSET_40) -> str:
    return build_cell_key("DOWN", eval_offset=offset, regime=_REGIME, window_ts=_TS_US_PM)


def _up_key(offset=_OFFSET_40) -> str:
    return build_cell_key("UP", eval_offset=offset, regime=_REGIME, window_ts=_TS_US_PM)


def _params(**extra) -> dict:
    p = {
        "min_offset_sec": 24,
        "max_offset_sec": 60,
        "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
        "block_down_vpin_regimes": ["NORMAL", "TRANSITION"],
        "block_up_vpin_regimes": ["NORMAL", "TRANSITION", "CASCADE"],
        "lgb_dist_min_down": 0.10,
        "lgb_dist_min_up": 1.01,
        "lgb_dist_min_down_with_hc_agree": 0.08,
        "lgb_dist_min_up_with_hc_agree": 1.01,
        "fill_band_min": 0.00,
        "fill_band_max": 0.82,
        "down_min_fill_price": 0.0,
        "up_min_fill_price": 1.01,
        "vpin_min": 0.0,
        "vpin_max": 1.0,
        "min_consecutive_pass_ticks": 1,
        "param_overrides_by_cell": {},
    }
    p.update(extra)
    return p


@pytest.fixture(autouse=True)
def _reset():
    reset_cooldown()
    reset_all_confirmations_v9()
    yield
    reset_cooldown()
    reset_all_confirmations_v9()


class TestV9CascadeFadeLateCellOverrides:

    # 1. Cell relaxes DOWN floor
    def test_cell_relaxes_down_floor(self):
        from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
        token = _gp.set_active(_params(param_overrides_by_cell={_down_key(): {"lgb_dist_min_down": 0.0}}))
        try:
            d = evaluate_v9_cascade_fade_late(_make_surface())
        finally:
            _gp.reset_active(token)
        assert d.strategy_id == "v9_cascade_fade_late"
        assert d.skip_reason != "lgb_safety_floor", f"Cell relax should open floor; got {d.skip_reason!r}"

    # 2. Cell tightens DOWN floor -> SKIP
    def test_cell_tightens_down_floor(self):
        from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
        token = _gp.set_active(_params(param_overrides_by_cell={_down_key(): {"lgb_dist_min_down": 0.40}}))
        try:
            d = evaluate_v9_cascade_fade_late(_make_surface(probability_lgb=0.25))
        finally:
            _gp.reset_active(token)
        assert d.action == "SKIP"
        assert "lgb_bucket" in (d.skip_reason or "") or "lgb_safety_floor" in (d.skip_reason or ""), (
            f"Tight cell should SKIP on conviction floor; got {d.skip_reason!r}"
        )
        assert d.strategy_id == "v9_cascade_fade_late"

    # 3. No cell match -> strategy floor applies
    def test_no_cell_match_uses_strategy_floor(self):
        from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
        token = _gp.set_active(_params(param_overrides_by_cell={_up_key(): {"lgb_dist_min_up": 0.99}}))
        try:
            d = evaluate_v9_cascade_fade_late(_make_surface())
        finally:
            _gp.reset_active(token)
        assert d.strategy_id == "v9_cascade_fade_late"
        assert d.skip_reason != "lgb_safety_floor"

    # 4. Empty overrides is no-op
    def test_empty_overrides_noop(self):
        from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
        token = _gp.set_active(_params(param_overrides_by_cell={}))
        try:
            d = evaluate_v9_cascade_fade_late(_make_surface())
        finally:
            _gp.reset_active(token)
        assert d.strategy_id == "v9_cascade_fade_late"
        assert d.skip_reason != "lgb_safety_floor"

    # 5. Only matching key fires
    def test_only_matching_key_fires(self):
        from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
        token = _gp.set_active(_params(param_overrides_by_cell={
            _down_key(): {"lgb_dist_min_down": 0.40},
            _up_key(): {"lgb_dist_min_up": 0.0},
        }))
        try:
            d = evaluate_v9_cascade_fade_late(_make_surface(probability_lgb=0.25))
        finally:
            _gp.reset_active(token)
        assert d.action == "SKIP"
        assert "lgb_bucket" in (d.skip_reason or "") or "lgb_safety_floor" in (d.skip_reason or ""), (
            f"DOWN key should block; got {d.skip_reason!r}"
        )

    # 6. Runtime tunability
    def test_runtime_tunability(self):
        from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
        # Phase 1: tight -> SKIP
        t1 = _gp.set_active(_params(param_overrides_by_cell={_down_key(): {"lgb_dist_min_down": 0.40}}))
        try:
            d1 = evaluate_v9_cascade_fade_late(_make_surface(probability_lgb=0.25))
        finally:
            _gp.reset_active(t1)
        assert "lgb_bucket" in (d1.skip_reason or "") or "lgb_safety_floor" in (d1.skip_reason or ""), (
            f"Phase 1 should block on conviction floor; got {d1.skip_reason!r}"
        )

        reset_all_confirmations_v9()

        # Phase 2: relaxed -> conviction gate not the blocker
        t2 = _gp.set_active(_params(param_overrides_by_cell={_down_key(): {"lgb_dist_min_down": 0.0}}))
        try:
            d2 = evaluate_v9_cascade_fade_late(_make_surface(probability_lgb=0.25))
        finally:
            _gp.reset_active(t2)
        assert d2.skip_reason != "lgb_safety_floor", f"Relaxed should not block; got {d2.skip_reason!r}"

    # 7. UP cell override relaxes lgb_dist_min_up
    def test_up_cell_override_relaxes_up_floor(self):
        from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
        token = _gp.set_active(_params(
            lgb_dist_min_up=0.0,
            block_up_vpin_regimes=[],
            param_overrides_by_cell={_up_key(): {"lgb_dist_min_up": 0.0}},
        ))
        try:
            d = evaluate_v9_cascade_fade_late(_make_surface(
                probability_lgb=0.80,
                poly_direction="UP", v4_macro_bias="BULL", v4_recommended_side="UP",
                delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
                delta_pct=0.005,
                clob_implied_up=0.70, gamma_up_price=0.70, gamma_down_price=0.30,
                cg_taker_buy_vol=1_200_000.0, cg_taker_sell_vol=800_000.0,
            ))
        finally:
            _gp.reset_active(token)
        assert d.strategy_id == "v9_cascade_fade_late"
        assert d.skip_reason != "lgb_safety_floor", f"UP override=0.0 should not produce lgb_safety_floor; got {d.skip_reason!r}"

    # 8. Override match stamps metadata
    def test_override_match_stamps_metadata(self):
        from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
        token = _gp.set_active(_params(param_overrides_by_cell={_down_key(): {"lgb_dist_min_down": 0.0}}))
        try:
            d = evaluate_v9_cascade_fade_late(_make_surface())
        finally:
            _gp.reset_active(token)
        meta = d.metadata or {}
        assert "cell_param_overrides_active" in meta, "cell_param_overrides_active must be in metadata on match"
        assert meta["cell_param_overrides_active"] == {"lgb_dist_min_down": 0.0}
        assert meta.get("cell_param_overrides_direction") == "DOWN"

    # 9. No match -> no metadata key
    def test_no_match_no_metadata_key(self):
        from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
        token = _gp.set_active(_params(param_overrides_by_cell={}))
        try:
            d = evaluate_v9_cascade_fade_late(_make_surface())
        finally:
            _gp.reset_active(token)
        assert "cell_param_overrides_active" not in (d.metadata or {})

    # 10. probability_lgb unavailable SKIP before cell override
    def test_probability_lgb_unavailable_skips_before_cell_override(self):
        from strategies.configs.v9_cascade_fade_late import evaluate_v9_cascade_fade_late
        token = _gp.set_active(_params(param_overrides_by_cell={_down_key(): {"lgb_dist_min_down": 0.0}}))
        try:
            d = evaluate_v9_cascade_fade_late(_make_surface(probability_lgb=None))
        finally:
            _gp.reset_active(token)
        assert d.action == "SKIP"
        assert "unavailable" in (d.skip_reason or "").lower() or d.skip_reason == "probability_lgb unavailable"
        assert "cell_param_overrides_active" not in (d.metadata or {})
