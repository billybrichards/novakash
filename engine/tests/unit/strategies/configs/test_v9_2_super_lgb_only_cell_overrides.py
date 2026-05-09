"""Unit tests for v9_2_super_lgb_only per-cell parameter override wiring.

Coverage:
  1. Cell relaxes DOWN floor — no lgb_safety_floor SKIP
  2. Cell tightens DOWN floor — base SKIP from v9_ensemble passes through
  3. Override match stamps `cell_param_overrides_active` in metadata
  4. No cell match — no metadata key
  5. Empty `param_overrides_by_cell` — no metadata key (no-op)
  6. UP cell override stamps direction=UP
  7. v9_2_model_not_loaded SKIP fires before cell-override resolution

Mirrors test_v9_1_cascade_fade_late_cell_overrides.py with the v9_2
qualifying-tick gate semantics — uses a low ticks_n_thresholds value so the
cohort gate doesn't pre-empt the base SKIP path under test.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from strategies import gate_params as _gp
from strategies.configs.v9_2_super_lgb_only import (
    evaluate_v9_2_super_lgb_only,
    reset_all_qualifying_ticks_v9_2,
)
from strategies.configs.v9_ensemble import (
    reset_all_confirmations_v9,
    reset_cooldown,
)
from strategies.data_surface import FullDataSurface
from strategies.gates.cell_param_overrides import build_cell_key


# Window resolves to hour=12 UTC (no per-direction blocks for either UP or DOWN).
_WTS = 1713009600  # 2024-04-13 12:00 UTC
_OFFSET = 120
_REGIME = "NORMAL"


def _make_surface(**overrides) -> FullDataSurface:
    """DOWN-aligned surface with high v9_2 conviction and oracle-aligned deltas."""
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=_WTS, eval_offset=_OFFSET, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime=_REGIME, twap_delta=-0.003,
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
        hour_utc=12, seconds_to_close=_OFFSET,
        probability_lgb_v9_2=0.10,  # DOWN, conviction 0.90
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _down_key() -> str:
    return build_cell_key("DOWN", eval_offset=_OFFSET, regime=_REGIME, window_ts=_WTS)


def _up_key() -> str:
    return build_cell_key("UP", eval_offset=_OFFSET, regime=_REGIME, window_ts=_WTS)


def _params(**extra) -> dict:
    p: dict[str, Any] = {
        "min_offset_sec": 30,
        "max_offset_sec": 200,
        "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
        "block_down_vpin_regimes": [],
        "block_up_vpin_regimes": [],
        "lgb_dist_min_down": 0.10,
        "lgb_dist_min_up": 0.15,
        "lgb_dist_min_up_with_hc_agree": 0.05,
        "lgb_dist_min_down_with_hc_agree": 0.05,
        "fill_band_min": 0.00,
        "fill_band_max": 0.82,
        "up_min_fill_price": 0.20,
        "down_min_fill_price": 0.15,
        "blocked_utc_hours": [],
        "blocked_utc_hours_up": [],
        "blocked_utc_hours_down": [],
        "source_agreement_require_chainlink": True,
        "source_agreement_require_tiingo": True,
        "skip_on_oracle_disagree": True,
        "vpin_min": 0.40,
        "vpin_max": 1.0,
        "post_loss_cooldown_min": 20,
        "ensemble_disagreement_threshold": 0.35,
        "require_direction_agreement": True,
        "pc_weight_t_60": 0.70, "pc_weight_t_120": 0.55,
        "pc_weight_t_180": 0.40, "pc_weight_t_200": 0.25,
        "vhc_threshold": 0.25, "vhc_bypass_transition": True,
        "vhc_bypass_up_dist": True, "vhc_bypass_disagreement": True,
        "vhc_bypass_lgb_safety_floor": False,  # don't bypass — we want to test the floor
        "vhc_bypass_oracle_direction": True, "vhc_kelly_multiplier": 2.0,
        "conviction_high_dist": 0.20, "conviction_medium_dist": 0.12,
        "conviction_low_dist": 0.05,
        "fallback_to_lgb_on_pc_null": True,
        "transition_strong_bypass_enabled": False,
        "delta_gate_enabled": False,
        "min_consecutive_pass_ticks": 0,
        "pl_vhc_bypass_enabled": False,
        "conviction_x_thresholds": {
            "TRANSITION_UP": 0.85, "TRANSITION_DOWN": 0.85,
            "CASCADE_UP": 0.85, "CASCADE_DOWN": 0.85,
            "NORMAL_UP": 0.85, "NORMAL_DOWN": 0.85, "CALM": None,
        },
        # Low N so the cohort gate doesn't pre-empt the base SKIP under test.
        "ticks_n_thresholds": {
            "TRANSITION_UP": 1, "TRANSITION_DOWN": 1,
            "CASCADE_UP": 1, "CASCADE_DOWN": 1,
            "NORMAL_UP": 1, "NORMAL_DOWN": 1,
        },
        "eval_offset_min": 60,
        "eval_offset_max": 300,
        "gate_min_wr_target": 0.90,
        # Disable sister veto so we can isolate cell-override behaviour.
        "sister_pair_veto": {"enabled": False, "pair": [], "agreement_window_seconds": 20},
        "param_overrides_by_cell": {},
    }
    p.update(extra)
    return p


@pytest.fixture(autouse=True)
def _reset():
    reset_cooldown()
    reset_all_confirmations_v9()
    reset_all_qualifying_ticks_v9_2()
    yield
    reset_cooldown()
    reset_all_confirmations_v9()
    reset_all_qualifying_ticks_v9_2()


class TestV92SuperCellOverrides:

    def test_cell_relaxes_down_floor(self):
        """Cell override sets lgb_dist_min_down=0.0 → no lgb_safety_floor SKIP."""
        token = _gp.set_active(_params(
            param_overrides_by_cell={_down_key(): {"lgb_dist_min_down": 0.0}},
        ))
        try:
            d = evaluate_v9_2_super_lgb_only(_make_surface())
        finally:
            _gp.reset_active(token)
        assert d.strategy_id == "v9_2_super_lgb_only"
        # Whatever happens downstream, the relaxed floor must not be the blocker.
        assert "lgb_safety_floor" not in (d.skip_reason or "")
        assert "lgb_bucket" not in (d.skip_reason or "")

    def test_cell_tightens_down_floor_blocks(self):
        """Cell override sets lgb_dist_min_down=0.50 → base SKIP from v9_ensemble."""
        token = _gp.set_active(_params(
            lgb_dist_min_down=0.10,
            param_overrides_by_cell={_down_key(): {"lgb_dist_min_down": 0.50}},
        ))
        try:
            # dist=0.40 (p=0.10) < 0.50 → must SKIP via lgb_safety_floor / lgb_bucket
            d = evaluate_v9_2_super_lgb_only(_make_surface())
        finally:
            _gp.reset_active(token)
        assert d.action == "SKIP"
        assert d.strategy_id == "v9_2_super_lgb_only"
        assert "lgb_safety_floor" in (d.skip_reason or "") or "lgb_bucket" in (d.skip_reason or ""), (
            f"Tight cell override should SKIP on conviction floor; got {d.skip_reason!r}"
        )

    def test_override_match_stamps_metadata(self):
        """Match → metadata has cell_param_overrides_active + direction."""
        override = {"lgb_dist_min_down": 0.50}
        token = _gp.set_active(_params(
            lgb_dist_min_down=0.10,
            param_overrides_by_cell={_down_key(): override},
        ))
        try:
            d = evaluate_v9_2_super_lgb_only(_make_surface())
        finally:
            _gp.reset_active(token)
        meta = d.metadata or {}
        assert meta.get("cell_param_overrides_active") == override
        assert meta.get("cell_param_overrides_direction") == "DOWN"

    def test_no_match_no_metadata_key(self):
        """UP override on DOWN surface → no metadata key."""
        token = _gp.set_active(_params(
            param_overrides_by_cell={_up_key(): {"lgb_dist_min_up": 0.0}},
        ))
        try:
            d = evaluate_v9_2_super_lgb_only(_make_surface())
        finally:
            _gp.reset_active(token)
        assert "cell_param_overrides_active" not in (d.metadata or {})

    def test_empty_overrides_noop(self):
        """Empty param_overrides_by_cell → no metadata key, strategy floor applies."""
        token = _gp.set_active(_params(param_overrides_by_cell={}))
        try:
            d = evaluate_v9_2_super_lgb_only(_make_surface())
        finally:
            _gp.reset_active(token)
        assert d.strategy_id == "v9_2_super_lgb_only"
        assert "cell_param_overrides_active" not in (d.metadata or {})

    def test_up_override_stamps_direction_up(self):
        """UP cell match → direction=UP in metadata."""
        override = {"lgb_dist_min_up": 0.50}
        token = _gp.set_active(_params(
            lgb_dist_min_up=0.15,
            param_overrides_by_cell={_up_key(): override},
        ))
        try:
            d = evaluate_v9_2_super_lgb_only(_make_surface(
                probability_lgb_v9_2=0.90, probability_lgb=0.90,
                delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
                delta_pct=0.005,
                poly_direction="UP", v4_macro_bias="BULL", v4_recommended_side="UP",
                clob_implied_up=0.65, gamma_up_price=0.65, gamma_down_price=0.35,
                cg_taker_buy_vol=1_200_000.0, cg_taker_sell_vol=800_000.0,
            ))
        finally:
            _gp.reset_active(token)
        meta = d.metadata or {}
        assert meta.get("cell_param_overrides_active") == override
        assert meta.get("cell_param_overrides_direction") == "UP"

    def test_model_not_loaded_skips_before_cell_override(self):
        """probability_lgb_v9_2=None → SKIP v9_2_model_not_loaded, no cell metadata."""
        token = _gp.set_active(_params(
            param_overrides_by_cell={_down_key(): {"lgb_dist_min_down": 0.0}},
        ))
        try:
            d = evaluate_v9_2_super_lgb_only(_make_surface(probability_lgb_v9_2=None))
        finally:
            _gp.reset_active(token)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_2_model_not_loaded"
        assert "cell_param_overrides_active" not in (d.metadata or {})
