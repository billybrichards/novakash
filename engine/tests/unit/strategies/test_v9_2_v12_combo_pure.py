"""Unit tests for v9_2_v12_combo_pure GHOST strategy.

Coverage (mirrors test_v9_2_v12_AND_ghost.py style — both probabilities required):
- model-not-loaded SKIP for each of v9_2_pure / v12_pure independently
- defensive asset guard (SKIP on non-BTC)
- eval_offset outside band [60, 210]
- UP requires BOTH v9_2_pure >= 0.75 AND v12_pure >= 0.70 (one alone is not enough)
- DOWN requires BOTH v9_2_pure <= 0.29 AND v12_pure <= 0.27
- conviction_below_threshold when either side fails the AND
- gate_params runtime override respected
- Metadata shape includes BOTH pure probabilities + thresholds
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_2_v12_combo_pure import evaluate_v9_2_v12_combo_pure
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    """Default surface: BTC, eval_offset=120, BOTH PURE probs above UP threshold."""
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
        probability_lgb_v9_2_pure=0.80,
        probability_lgb_v12_pure=0.75,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


_BASE_PARAMS: dict[str, Any] = {
    "up_v92_threshold": 0.75,
    "up_v12_threshold": 0.70,
    "down_v92_threshold": 0.29,
    "down_v12_threshold": 0.27,
    "eval_offset_min": 60,
    "eval_offset_max": 210,
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "BTC",
    "entry_cap": 0.93,
    "collateral_pct": 0.025,
    "gtc_cap": 0.96,
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
    from strategies.configs import v9_2_v12_combo_pure
    v9_2_v12_combo_pure._consec_state.clear()
    yield
    v9_2_v12_combo_pure._consec_state.clear()


# ── Forward-compat: timesfm side not emitting yet ──────────────────────────


class TestForwardCompat:
    def test_v9_2_pure_missing_skips(self):
        surface = _make_surface(probability_lgb_v9_2_pure=None)
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_2_pure_model_not_loaded"
        assert d.strategy_id == "v9_2_v12_combo_pure"

    def test_v12_pure_missing_skips(self):
        surface = _make_surface(probability_lgb_v12_pure=None)
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v12_pure_model_not_loaded"

    def test_both_missing_skips_with_v9_2_first(self):
        surface = _make_surface(
            probability_lgb_v9_2_pure=None,
            probability_lgb_v12_pure=None,
        )
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        # v9_2 is checked first.
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_2_pure_model_not_loaded"


# ── Defensive asset guard ──────────────────────────────────────────────────


class TestAssetGuard:
    def test_eth_skips(self):
        surface = _make_surface(asset="ETH")
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_skips(self):
        surface = _make_surface(asset="XRP")
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band [60, 210] ─────────────────────────────────────────────


class TestEvalOffsetBand:
    def test_below_min_skips(self):
        surface = _make_surface(eval_offset=50)
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_above_max_210_skips(self):
        surface = _make_surface(eval_offset=220)
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "outside_eval_band"

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60)
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_210_fires(self):
        surface = _make_surface(eval_offset=210)
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"


# ── AND-combo gating (critical: BOTH probs required) ───────────────────────


class TestANDComboUP:
    def test_both_above_up_thresholds_fires(self):
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.80, probability_lgb_v12_pure=0.75,
        )
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.entry_reason == "v9_2_v12_combo_pure_pass"

    def test_at_thresholds_exact_fires(self):
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.75, probability_lgb_v12_pure=0.70,
        )
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_v9_2_above_v12_below_skips(self):
        """The critical AND test — strong v9.2 alone is NOT enough."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.95, probability_lgb_v12_pure=0.65,
        )
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_v9_2_below_v12_above_skips(self):
        """Strong v12 alone is NOT enough."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.70, probability_lgb_v12_pure=0.95,
        )
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


class TestANDComboDOWN:
    def test_both_below_down_thresholds_fires(self):
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.20, probability_lgb_v12_pure=0.20,
        )
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_at_down_thresholds_exact_fires(self):
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.29, probability_lgb_v12_pure=0.27,
        )
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_v9_2_below_v12_above_down_skips(self):
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.20, probability_lgb_v12_pure=0.40,
        )
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Runtime gate_params overrides ──────────────────────────────────────────


class TestGateParamOverrides:
    def test_runtime_relax_lets_a_mid_signal_fire(self):
        """Relax UP to (0.60, 0.55) — a (0.62, 0.58) signal should fire."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.62, probability_lgb_v12_pure=0.58,
        )
        with _params(up_v92_threshold=0.60, up_v12_threshold=0.55):
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_runtime_tighten_blocks_default_passing_signal(self):
        """Tighten UP to (0.95, 0.90) — default-passing (0.80, 0.75) should skip."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.80, probability_lgb_v12_pure=0.75,
        )
        with _params(up_v92_threshold=0.95, up_v12_threshold=0.90):
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Metadata shape on TRADE ────────────────────────────────────────────────


class TestMetadataShape:
    def test_trade_metadata_includes_both_pure_probs(self):
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.85, probability_lgb_v12_pure=0.80,
        )
        with _params():
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        # CRITICAL: metadata MUST include BOTH PURE column names (not the
        # blended counterparts). The engine + analytics rely on this to
        # disambiguate which columns triggered the fire.
        assert "probability_lgb_v9_2_pure" in d.metadata
        assert "probability_lgb_v12_pure" in d.metadata
        assert d.metadata["probability_lgb_v9_2_pure"] == pytest.approx(0.85)
        assert d.metadata["probability_lgb_v12_pure"] == pytest.approx(0.80)
        assert d.metadata["up_v92_threshold"] == pytest.approx(0.75)
        assert d.metadata["up_v12_threshold"] == pytest.approx(0.70)
        assert d.metadata["down_v92_threshold"] == pytest.approx(0.29)
        assert d.metadata["down_v12_threshold"] == pytest.approx(0.27)
        assert d.metadata["entry_cap"] == pytest.approx(0.93)
        assert d.metadata["gtc_cap"] == pytest.approx(0.96)


# ── Direction-aware fill-band gate (RDS note #664, 2026-05-25) ─────────────


class TestDirectionAwareFillBandGate:
    """entry_floor_up=0.60 blocks UP fires at fill < 0.60.
    entry_cap_down=0.90 blocks DOWN fires at fill >= 0.90.
    The gate is DIRECTION-AWARE — it does NOT mirror: UP at fill>=0.90 is
    100% WR (5W/0L today) and DOWN at fill<0.60 is 100% WR (3W/0L today);
    a symmetric band would wrongly block those wins.
    """

    # ── UP gate (entry_floor_up) ────────────────────────────────────────────

    def test_up_fill_below_floor_skips(self):
        """UP fire at fill=0.55 < 0.60 — should SKIP (25% WR zone per RDS #664)."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.80,
            probability_lgb_v12_pure=0.75,
            clob_implied_up=0.55,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_below_up_floor" in d.skip_reason
        assert "0.550" in d.skip_reason
        assert "0.600" in d.skip_reason

    def test_up_fill_at_floor_exact_trades(self):
        """UP fire at fill=0.60 exactly — boundary inclusive, should TRADE."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.80,
            probability_lgb_v12_pure=0.75,
            clob_implied_up=0.60,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_fill_above_floor_trades(self):
        """UP fire at fill=0.65 (well above floor) — TRADE."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.80,
            probability_lgb_v12_pure=0.75,
            clob_implied_up=0.65,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_up_high_fill_above_090_still_trades(self):
        """UP at fill=0.92 (above 0.90) must TRADE — 100% WR zone (5W/0L today).
        Symmetric gate would wrongly block this; direction-aware gate does NOT."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.80,
            probability_lgb_v12_pure=0.75,
            clob_implied_up=0.92,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    # ── DOWN gate (entry_cap_down) ──────────────────────────────────────────

    def test_down_fill_at_cap_skips(self):
        """DOWN fire at fill=0.90 (>= 0.90) — should SKIP (33% WR per RDS #664)."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.20,
            probability_lgb_v12_pure=0.20,
            clob_implied_up=0.90,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "SKIP"
        assert d.skip_reason is not None
        assert "fill_above_down_cap" in d.skip_reason
        assert "0.900" in d.skip_reason

    def test_down_fill_just_below_cap_trades(self):
        """DOWN fire at fill=0.89 (< 0.90) — boundary inclusive on allow side, TRADE."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.20,
            probability_lgb_v12_pure=0.20,
            clob_implied_up=0.89,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_low_fill_below_060_still_trades(self):
        """DOWN at fill=0.55 (below 0.60) must TRADE — 100% WR zone (3W/0L today).
        Symmetric gate would wrongly block this; direction-aware gate does NOT."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.20,
            probability_lgb_v12_pure=0.20,
            clob_implied_up=0.55,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    # ── Default permissive: strats without these params unaffected ──────────

    def test_no_fill_price_skips_gate_entirely(self):
        """When clob_implied_up is None, fill gate is bypassed — TRADE."""
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.80,
            probability_lgb_v12_pure=0.75,
            clob_implied_up=None,
        )
        with _params(entry_floor_up=0.60, entry_cap_down=0.90):
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_default_permissive_params_allow_all_fills(self):
        """entry_floor_up=0.0, entry_cap_down=1.0 (defaults) allow all fills."""
        # Very low fill — would be blocked by explicit floor but not by defaults.
        surface = _make_surface(
            probability_lgb_v9_2_pure=0.80,
            probability_lgb_v12_pure=0.75,
            clob_implied_up=0.10,
        )
        with _params():  # no entry_floor_up / entry_cap_down set
            d = evaluate_v9_2_v12_combo_pure(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
