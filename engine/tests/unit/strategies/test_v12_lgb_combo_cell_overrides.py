"""Unit tests for v12_lgb_combo combo_min_dist per-cell override (hub #402, PR #506).

Coverage:
  1. Cell override below global floor -> TRADE (cell relaxes floor)
  2. Cell override above global floor -> SKIP (cell tightens floor)
  3. No cell match -> falls back to global combo_min_dist
  4. Empty param_overrides_by_cell -> no-op (global floor applies)
  5. Multiple cells in map, only matching one fires
  6. Runtime-tunability: override changes mid-session via gate_params contextvar
  7. Cell override with zero -> accepts dist=0.0+ (full relaxation)
  8. Disagreement path unaffected by cell overrides (cell override is agreement-path only)
"""
from __future__ import annotations

import os
import sys
import time

import pytest

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

# Set required env vars before any engine imports trigger Settings()
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from strategies.data_surface import FullDataSurface
from strategies import gate_params as _gp
from strategies.gates.cell_param_overrides import build_cell_key


# -- Test surface factory -------------------------------------------------
# window_ts that resolves to us_pm (14-17 UTC) -- 2026-05-08 15:00:00 UTC
_TS_US_PM = 1746716400
# eval_offset that resolves to T-121-180 (121-180s remaining)
_OFFSET_T121 = 150
# VPIN regime label used for the matching key
_REGIME = "TRANSITION"


def _make_surface(**overrides) -> FullDataSurface:
    """Build a FullDataSurface suitable for combo-path (both models present,
    same direction, dist above default 0.10 floor unless overridden)."""
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=_TS_US_PM,
        eval_offset=_OFFSET_T121, assembled_at=time.time(),
        current_price=84500.0, open_price=84000.0,
        delta_binance=0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.004, delta_source="tiingo_rest_candle",
        vpin=0.55, regime=_REGIME, twap_delta=-0.003,
        v2_probability_up=0.38, v2_probability_raw=0.36,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=None, probability_classifier=None, ensemble_config=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None, v3_sub_momentum=None,
        v4_regime="calm_trend", v4_regime_confidence=0.85, v4_regime_persistence=0.9,
        v4_macro_bias="BEAR", v4_macro_direction_gate="ALLOW_ALL", v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.38,
        poly_confidence_distance=0.12, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.46, clob_up_ask=0.48, clob_down_bid=0.32,
        clob_down_ask=0.34, clob_implied_up=0.47,
        gamma_up_price=0.45, gamma_down_price=0.35,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=15, seconds_to_close=150,
        # DOWN-direction combo by default: both v9.1 and v12 say DOWN
        probability_lgb_v9_1=0.25,   # dist_v9_1 = 0.25
        probability_lgb_v12=0.20,    # dist_v12  = 0.30, combo_dist = min(0.25,0.30) = 0.25
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _cell_key_for_surface(direction: str = "DOWN") -> str:
    return build_cell_key(
        direction=direction,
        eval_offset=_OFFSET_T121,
        regime=_REGIME,
        window_ts=_TS_US_PM,
    )


class TestV12LgbComboCellOverrides:
    """Per-cell combo_min_dist override tests for v12_lgb_combo (PR #506)."""

    # -- Test 1: cell relaxes floor, trade should not be blocked by conv floor
    def test_cell_override_relaxes_combo_floor(self):
        """Cell override sets combo_min_dist=0.0 for this exact cell.
        combo_dist=0.25 easily clears 0.0 -> gate does not fire for
        combo_below_conviction_floor. (v9_ensemble stack may SKIP for other
        reasons — we just assert the override is respected, not that it trades.)
        """
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        cell_key = _cell_key_for_surface("DOWN")
        overrides_map = {cell_key: {"combo_min_dist": 0.0}}
        token = _gp.set_active({"param_overrides_by_cell": overrides_map})
        try:
            surface = _make_surface()  # combo_dist=0.25, well above 0.0
            decision = evaluate_v12_lgb_combo(surface)
        finally:
            _gp.reset_active(token)

        # Must NOT skip for combo_below_conviction_floor
        assert decision.skip_reason != "combo_below_conviction_floor", (
            f"Cell override (0.0) should have relaxed the floor; got skip_reason={decision.skip_reason!r}"
        )
        assert decision.strategy_id == "v12_lgb_combo"

    # -- Test 2: cell tightens floor, combo_dist is below the tighter threshold
    def test_cell_override_tightens_combo_floor(self):
        """Cell override sets combo_min_dist=0.40 for this cell.
        combo_dist=0.25 is below 0.40 -> SKIP combo_below_conviction_floor.
        """
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        cell_key = _cell_key_for_surface("DOWN")
        overrides_map = {cell_key: {"combo_min_dist": 0.40}}
        token = _gp.set_active({"param_overrides_by_cell": overrides_map})
        try:
            # combo_dist = min(0.25, 0.30) = 0.25 < 0.40
            surface = _make_surface()
            decision = evaluate_v12_lgb_combo(surface)
        finally:
            _gp.reset_active(token)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "combo_below_conviction_floor"
        assert decision.metadata["combo_min_dist"] == 0.40
        assert decision.strategy_id == "v12_lgb_combo"

    # -- Test 3: no matching cell key -> falls back to global combo_min_dist
    def test_no_cell_match_uses_global_floor(self):
        """A cell key for a different direction does not match.
        Falls back to global combo_min_dist (default 0.10).
        combo_dist=0.25 > 0.10, so gate should not fire on conviction floor.
        """
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        # Put an UP override in for this cell -- but our surface is DOWN
        wrong_key = _cell_key_for_surface("UP")
        overrides_map = {wrong_key: {"combo_min_dist": 0.50}}  # very high floor
        token = _gp.set_active({"param_overrides_by_cell": overrides_map})
        try:
            surface = _make_surface()  # DOWN direction
            decision = evaluate_v12_lgb_combo(surface)
        finally:
            _gp.reset_active(token)

        # The wrong-direction override should NOT affect DOWN evaluation.
        # The global floor (0.10) applies and combo_dist=0.25 clears it.
        assert decision.skip_reason != "combo_below_conviction_floor"
        assert decision.strategy_id == "v12_lgb_combo"

    # -- Test 4: empty param_overrides_by_cell -> no-op
    def test_empty_overrides_map_is_noop(self):
        """Empty map -> no cell match -> global combo_min_dist (0.10) applies."""
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        token = _gp.set_active({"param_overrides_by_cell": {}})
        try:
            surface = _make_surface()
            decision = evaluate_v12_lgb_combo(surface)
        finally:
            _gp.reset_active(token)

        # combo_dist=0.25 > default 0.10 -> conviction floor should not block
        assert decision.skip_reason != "combo_below_conviction_floor"
        assert decision.strategy_id == "v12_lgb_combo"

    # -- Test 5: multiple cells in map, only the exact match fires
    def test_only_matching_cell_key_fires(self):
        """Map has both UP and DOWN keys; DOWN surface only uses DOWN key."""
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        down_key = _cell_key_for_surface("DOWN")
        up_key = _cell_key_for_surface("UP")
        overrides_map = {
            down_key: {"combo_min_dist": 0.40},   # will block DOWN
            up_key: {"combo_min_dist": 0.0},       # would relax UP (irrelevant here)
        }
        token = _gp.set_active({"param_overrides_by_cell": overrides_map})
        try:
            surface = _make_surface()  # DOWN
            decision = evaluate_v12_lgb_combo(surface)
        finally:
            _gp.reset_active(token)

        # DOWN key with combo_min_dist=0.40 should block (combo_dist=0.25 < 0.40)
        assert decision.action == "SKIP"
        assert decision.skip_reason == "combo_below_conviction_floor"
        assert decision.metadata["combo_min_dist"] == 0.40

    # -- Test 6: runtime-tunability via gate_params contextvar
    def test_runtime_tunability_via_gate_params(self):
        """Changing the override dict in gate_params context affects outcome
        without touching the code — confirms fail-open / runtime-tunable design.
        """
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        cell_key = _cell_key_for_surface("DOWN")

        # Phase 1: tight override -> SKIP
        tight_map = {cell_key: {"combo_min_dist": 0.40}}
        token1 = _gp.set_active({"param_overrides_by_cell": tight_map})
        try:
            surface = _make_surface()
            d1 = evaluate_v12_lgb_combo(surface)
        finally:
            _gp.reset_active(token1)

        assert d1.skip_reason == "combo_below_conviction_floor"

        # Phase 2: same surface, relaxed override -> conviction floor not the blocker
        loose_map = {cell_key: {"combo_min_dist": 0.0}}
        token2 = _gp.set_active({"param_overrides_by_cell": loose_map})
        try:
            surface2 = _make_surface()
            d2 = evaluate_v12_lgb_combo(surface2)
        finally:
            _gp.reset_active(token2)

        assert d2.skip_reason != "combo_below_conviction_floor"

    # -- Test 7: zero override fully relaxes the floor
    def test_zero_override_fully_relaxes_floor(self):
        """combo_min_dist=0.0 means any positive combo_dist will pass the gate.
        Test with very low combo_dist (0.01) that would be blocked by default floor.
        """
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        # Make combo_dist very small: both close to 0.5
        # p_v9_1=0.49 -> dist=0.01, p_v12=0.48 -> dist=0.02, combo_dist=0.01
        cell_key = _cell_key_for_surface("DOWN")
        overrides_map = {cell_key: {"combo_min_dist": 0.0}}
        token = _gp.set_active({"param_overrides_by_cell": overrides_map})
        try:
            surface = _make_surface(
                probability_lgb_v9_1=0.49,  # dist=0.01, dir=DOWN
                probability_lgb_v12=0.48,   # dist=0.02, dir=DOWN
            )
            decision = evaluate_v12_lgb_combo(surface)
        finally:
            _gp.reset_active(token)

        # With floor=0.0, combo_dist=0.01 should pass the conviction gate
        assert decision.skip_reason != "combo_below_conviction_floor", (
            f"zero override should fully relax floor; skip_reason={decision.skip_reason!r}"
        )

    # -- Test 8: disagreement path is not affected by cell overrides
    def test_disagreement_path_unaffected_by_cell_override(self):
        """When v9.1 and v12 disagree direction, the combo_min_dist cell
        override in the agreement path does not interfere with Option C logic.
        The disagreement path uses v12_contrarian_min_dist, not combo_min_dist.
        """
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        # Build a disagreement surface: v9.1 says UP, v12 says DOWN
        # We put a tight combo_min_dist override in the DOWN cell -- this
        # should be irrelevant because Option C kicks in (directions disagree).
        down_key = _cell_key_for_surface("DOWN")
        overrides_map = {down_key: {"combo_min_dist": 0.99}}  # would block agreement
        token = _gp.set_active({"param_overrides_by_cell": overrides_map})
        try:
            surface = _make_surface(
                probability_lgb_v9_1=0.80,  # UP, dist=0.30
                probability_lgb_v12=0.20,   # DOWN, dist=0.30
            )
            decision = evaluate_v12_lgb_combo(surface)
        finally:
            _gp.reset_active(token)

        # The combo_below_conviction_floor skip reason only fires in Option A.
        # Here we are in Option C (disagreement), so that skip reason must NOT appear.
        assert decision.skip_reason != "combo_below_conviction_floor", (
            "combo_min_dist cell override should not affect Option C disagreement path"
        )
        assert decision.strategy_id == "v12_lgb_combo"
        # Confirm we are in disagreement metadata territory
        assert decision.metadata.get("direction_agree") is False
