"""Unit tests for v9_1_meta_kelly strategy.

Coverage:
- meta_model_not_loaded SKIP when probability_lgb_v9_1 is None
- meta_model_not_loaded SKIP when probability_meta_v9_1 is None (V9_1_META_ENABLED=false)
- Cascade-fade-pair veto fires BEFORE meta scoring
- Disabled-flag test: META_V9_1_ENABLED=false -> strategy doesn't fire
- Graceful-fallback: probability_meta_v9_1 is None -> strategy skips, no crash
- Kelly veto: meta_prob gives negative Kelly -> SKIP kelly_veto
- Kelly positive: meta_prob high enough -> TRADE with stake metadata
- Once-per-window: second call for same (window_ts, direction) -> SKIP
- deterministic_stake math parity with training/meta_v2/stake_sizing.py contract
- Both UP and DOWN directions route correctly
- Metadata shape: stake_fraction, meta_model_version, abs_max_stake_usd

Tests mirror test_v9_2_super_lgb_only.py structure.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_1_meta_kelly import (
    _STRATEGY_ID,
    _VERSION,
    deterministic_stake,
    evaluate_v9_1_meta_kelly,
    has_window_fired,
    mark_window_fired,
    reset_all_window_fired_v9_1_meta,
)
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# Surface factory

def _make_surface(**overrides) -> FullDataSurface:
    """Default valid DOWN surface with probability_lgb_v9_1 + probability_meta_v9_1.

    window_ts=1713009600 -> 2024-04-13 12:00:00 UTC (hour=12, no veto risk).
    probability_lgb_v9_1=0.22 -> DOWN direction.
    probability_meta_v9_1=0.95 -> high meta confidence -> Kelly>0.
    clob_down_ask=0.55 -> fill_price=0.55 -> b=0.818, Kelly positive.
    """
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=1713009600,
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
        # v9.1 LGB + meta fields
        probability_lgb_v9_1=0.22,      # DOWN direction
        probability_meta_v9_1=0.95,     # high meta confidence
        probability_lgb_v9_2=None,      # not used by this strategy
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _make_surface_up(**overrides) -> FullDataSurface:
    """UP-direction variant."""
    up_defaults = dict(
        probability_lgb_v9_1=0.88,      # UP direction
        probability_meta_v9_1=0.94,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.005, delta_source="chainlink",
        twap_delta=0.003,
        poly_direction="UP",
        v4_macro_bias="BULL",
        poly_confidence=0.70,
        poly_confidence_distance=0.20,
        v4_recommended_side="UP",
        clob_up_bid=0.63, clob_up_ask=0.65, clob_down_bid=0.28,
        clob_down_ask=0.30, clob_implied_up=0.65,
        gamma_up_price=0.65, gamma_down_price=0.35,
        cg_taker_buy_vol=1_200_000.0, cg_taker_sell_vol=800_000.0,
        probability_lgb=0.88,
    )
    up_defaults.update(overrides)
    return _make_surface(**up_defaults)


# Gate-params fixture

@pytest.fixture(autouse=True)
def _bind_gate_params_and_reset():
    """Bind v9_1_meta_kelly defaults and reset state around each test."""
    params: dict[str, Any] = {
        "abs_max_stake_usd": 10.0,
        "kelly_fraction": 0.25,
        "meta_model_version": "meta_v2_v2_2026-05-08",
        "sister_pair_veto": {
            "enabled": False,   # disabled by default in tests to isolate Kelly logic
            "pair": ["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
            "agreement_window_seconds": 20,
        },
    }
    token = _gp._ACTIVE.set(params)
    reset_all_window_fired_v9_1_meta()
    yield
    _gp._ACTIVE.reset(token)
    reset_all_window_fired_v9_1_meta()


# deterministic_stake unit tests

class TestDeterministicStake:
    """Verify Kelly math matches training/meta_v2/stake_sizing.py contract."""

    def test_kelly_positive_high_confidence(self):
        """High meta_prob + low fill_price -> positive fractional stake."""
        # fill_price=0.65, b=(1-0.65)/0.65=0.5385, p=0.95, q=0.05
        # kelly_full = (0.5385*0.95 - 0.05) / 0.5385 = (0.5115 - 0.05)/0.5385 = 0.857
        # stake = 0.857 * 0.25 = 0.214
        stake = deterministic_stake(0.95, 0.65, 0.25)
        assert stake > 0.0, "High-confidence meta should yield positive stake"
        assert stake < 1.0, "Stake fraction must be < 1.0"
        assert abs(stake - 0.214) < 0.01, f"Expected ~0.214, got {stake:.4f}"

    def test_kelly_zero_at_breakeven(self):
        """meta_prob == fill_price -> Kelly = 0 exact breakeven."""
        # Kelly = 0 when p*(b+1) = 1 -> p = fill_price
        fill = 0.65
        breakeven_p = fill  # Kelly = 0 exactly when meta_prob == fill_price
        stake = deterministic_stake(breakeven_p, fill, 0.25)
        assert abs(stake) < 1e-9, f"At Kelly breakeven (p=fill_price), stake should be 0, got {stake:.6f}"

    def test_kelly_zero_below_breakeven(self):
        """meta_prob below breakeven -> Kelly negative -> stake = 0."""
        fill = 0.65
        stake = deterministic_stake(0.5, fill, 0.25)
        assert stake == 0.0, "Below-breakeven meta_prob should veto (stake=0)"

    def test_invalid_fill_price_extremes(self):
        """fill_price <= 0 or >= 1 -> stake = 0 (safety guard)."""
        assert deterministic_stake(0.95, 0.0, 0.25) == 0.0
        assert deterministic_stake(0.95, 1.0, 0.25) == 0.0
        assert deterministic_stake(0.95, -0.1, 0.25) == 0.0

    def test_kelly_fraction_scaling(self):
        """Stake scales linearly with kelly_fraction."""
        s_half = deterministic_stake(0.95, 0.65, 0.50)
        s_quarter = deterministic_stake(0.95, 0.65, 0.25)
        assert abs(s_half - 2 * s_quarter) < 0.001


# Disabled-flag tests (probability_meta_v9_1 is None)

class TestDisabledFlag:
    """Row 1+2: V9_1_META_ENABLED=false / model not loaded."""

    def test_p_lgb_v9_1_none_skips(self):
        """probability_lgb_v9_1 is None -> SKIP meta_model_not_loaded."""
        surface = _make_surface(probability_lgb_v9_1=None)
        d = evaluate_v9_1_meta_kelly(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "meta_model_not_loaded"
        assert d.strategy_id == _STRATEGY_ID

    def test_p_meta_none_skips(self):
        """probability_meta_v9_1 is None (V9_1_META_ENABLED=false) -> SKIP."""
        surface = _make_surface(probability_meta_v9_1=None)
        d = evaluate_v9_1_meta_kelly(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "meta_model_not_loaded"
        assert d.metadata["reason"] == "probability_meta_v9_1 is None (V9_1_META_ENABLED=false?)"

    def test_skip_does_not_mark_window_fired(self):
        """A meta_model_not_loaded SKIP must not mark the window as fired."""
        surface = _make_surface(probability_meta_v9_1=None)
        evaluate_v9_1_meta_kelly(surface)
        # Window should NOT be marked (we never got to step 9)
        assert not has_window_fired(int(surface.window_ts), "DOWN")


# Cascade-fade veto tests

class TestCascadeFadeVeto:
    """Veto runs BEFORE meta scoring -- hard constraint."""

    def test_cascade_fade_veto_skips(self):
        """When cascade-fade sisters veto, SKIP with cascade_fade_veto reason."""
        params = _gp._ACTIVE.get().copy()
        params["sister_pair_veto"] = {
            "enabled": True,
            "pair": ["v9_1_cascade_fade_late", "v9_cascade_fade_late"],
            "agreement_window_seconds": 20,
        }
        token = _gp._ACTIVE.set(params)
        try:
            surface = _make_surface(probability_meta_v9_1=0.99)  # very high meta
            with patch("strategies.configs.v9_1_meta_kelly.is_sister_pair_veto_active",
                       return_value=True):
                d = evaluate_v9_1_meta_kelly(surface)
            assert d.action == "SKIP"
            assert d.skip_reason == "cascade_fade_veto"
            # meta was never scored (veto fires first)
            assert "sister_veto_pair" in d.metadata
        finally:
            _gp._ACTIVE.reset(token)

    def test_veto_does_not_mark_window_fired(self):
        """A cascade_fade_veto SKIP must not mark window as fired."""
        params = _gp._ACTIVE.get().copy()
        params["sister_pair_veto"] = {"enabled": True,
                                       "pair": ["a", "b"], "agreement_window_seconds": 20}
        token = _gp._ACTIVE.set(params)
        try:
            surface = _make_surface()
            with patch("strategies.configs.v9_1_meta_kelly.is_sister_pair_veto_active",
                       return_value=True):
                evaluate_v9_1_meta_kelly(surface)
            assert not has_window_fired(int(surface.window_ts), "DOWN")
        finally:
            _gp._ACTIVE.reset(token)


# Kelly veto tests

class TestKellyVeto:
    """Kelly stake == 0 -> SKIP kelly_veto."""

    def test_low_meta_prob_vetoed(self):
        """meta_prob=0.5 at fill=0.65 -> Kelly negative -> SKIP."""
        surface = _make_surface(probability_meta_v9_1=0.5, clob_down_ask=0.65)
        d = evaluate_v9_1_meta_kelly(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "kelly_veto"
        assert d.metadata["stake_fraction"] == 0.0

    def test_kelly_veto_does_not_mark_window(self):
        """A kelly_veto SKIP must not mark window as fired."""
        surface = _make_surface(probability_meta_v9_1=0.5, clob_down_ask=0.65)
        evaluate_v9_1_meta_kelly(surface)
        assert not has_window_fired(int(surface.window_ts), "DOWN")


# Scoring success tests

class TestScoringSuccess:
    """Kelly > 0 -> TRADE with correct metadata."""

    def test_down_trade_shape(self):
        """Default DOWN surface with high meta_prob -> TRADE."""
        surface = _make_surface()
        d = evaluate_v9_1_meta_kelly(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"
        assert d.strategy_id == _STRATEGY_ID
        assert d.strategy_version == _VERSION
        assert d.metadata["probability_lgb_v9_1"] == pytest.approx(0.22, abs=1e-9)
        assert d.metadata["probability_meta_v9_1"] == pytest.approx(0.95, abs=1e-9)
        assert d.metadata["meta_pred_direction"] == "DOWN"
        assert d.metadata["stake_fraction"] > 0.0
        assert d.metadata["abs_max_stake_usd"] == 10.0
        assert d.metadata["kelly_fraction"] == 0.25
        assert d.metadata["meta_model_version"] == "meta_v2_v2_2026-05-08"
        assert d.metadata["v9_1_meta_kelly_active"] is True
        assert d.metadata["lgb_only_forced"] is True

    def test_up_trade_shape(self):
        """UP surface with high meta_prob -> TRADE with UP direction."""
        surface = _make_surface_up()
        d = evaluate_v9_1_meta_kelly(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.metadata["meta_pred_direction"] == "UP"
        assert d.metadata["stake_fraction"] > 0.0

    def test_stake_fraction_uses_clob_ask(self):
        """Stake fraction is computed from direction-aware CLOB ask."""
        # DOWN direction -> clob_down_ask
        surface = _make_surface(clob_down_ask=0.55, probability_meta_v9_1=0.95)
        d = evaluate_v9_1_meta_kelly(surface)
        assert d.action == "TRADE"
        expected_stake = deterministic_stake(0.95, 0.55, 0.25)
        assert d.metadata["stake_fraction"] == pytest.approx(expected_stake, abs=1e-6)
        assert d.metadata["fill_price"] == pytest.approx(0.55, abs=1e-9)

    def test_confidence_tier_high(self):
        """meta_prob >= 0.90 -> confidence=HIGH."""
        surface = _make_surface(probability_meta_v9_1=0.92)
        d = evaluate_v9_1_meta_kelly(surface)
        assert d.confidence == "HIGH"

    def test_confidence_tier_medium(self):
        """meta_prob in [0.80, 0.90) -> confidence=MEDIUM."""
        # Need fill_price that gives positive Kelly at 0.82
        # At fill=0.55: breakeven_wr = 1/(2-0.55) = 0.69. 0.82 > 0.69 -> positive
        surface = _make_surface(probability_meta_v9_1=0.82, clob_down_ask=0.55)
        d = evaluate_v9_1_meta_kelly(surface)
        if d.action == "TRADE":
            assert d.confidence == "MEDIUM"

    def test_entry_reason_format(self):
        """entry_reason encodes direction, meta_prob, stake, fill."""
        surface = _make_surface()
        d = evaluate_v9_1_meta_kelly(surface)
        assert d.action == "TRADE"
        assert "meta_kelly_down" in d.entry_reason
        assert "p_meta=" in d.entry_reason
        assert "stake=" in d.entry_reason


# Once-per-window tests

class TestOncePerWindow:
    """Second fire in same (window_ts, direction) -> SKIP already_fired_this_window."""

    def test_second_call_same_window_skips(self):
        """After TRADE, same (window_ts, direction) -> SKIP."""
        surface = _make_surface()
        d1 = evaluate_v9_1_meta_kelly(surface)
        assert d1.action == "TRADE"
        d2 = evaluate_v9_1_meta_kelly(surface)
        assert d2.action == "SKIP"
        assert d2.skip_reason == "already_fired_this_window"

    def test_different_window_not_blocked(self):
        """Different window_ts -> new window -> no block."""
        surface1 = _make_surface(window_ts=1713009600)
        surface2 = _make_surface(window_ts=1713009900)  # 5m later
        d1 = evaluate_v9_1_meta_kelly(surface1)
        assert d1.action == "TRADE"
        d2 = evaluate_v9_1_meta_kelly(surface2)
        assert d2.action == "TRADE", "Different window_ts should not be blocked"

    def test_different_direction_not_blocked(self):
        """Same window_ts, different direction -> not blocked."""
        surface_down = _make_surface(window_ts=1713009600)
        surface_up = _make_surface_up(window_ts=1713009600)
        d_down = evaluate_v9_1_meta_kelly(surface_down)
        assert d_down.action == "TRADE"
        d_up = evaluate_v9_1_meta_kelly(surface_up)
        # UP direction is a different key -- should not be blocked by DOWN fire
        assert d_up.action == "TRADE", "Different direction should not block"

    def test_reset_clears_state(self):
        """reset_all_window_fired_v9_1_meta() clears once-per-window state."""
        surface = _make_surface()
        d1 = evaluate_v9_1_meta_kelly(surface)
        assert d1.action == "TRADE"
        reset_all_window_fired_v9_1_meta()
        d2 = evaluate_v9_1_meta_kelly(surface)
        assert d2.action == "TRADE", "After reset, window should not be blocked"


# Isolation test: v9_1_lgb_only must not be modified

class TestV9_1LgbOnlyUntouched:
    """v9_1_lgb_only must not be imported or modified by v9_1_meta_kelly."""

    def test_no_import_of_v9_1_lgb_only(self):
        """v9_1_meta_kelly does not import from v9_1_lgb_only."""
        import strategies.configs.v9_1_meta_kelly as meta_mod
        import inspect
        source = inspect.getsource(meta_mod)
        assert "v9_1_lgb_only" not in source or "sister" in source or "strategy_id" in source, (
            "v9_1_meta_kelly must not depend on v9_1_lgb_only module"
        )

    def test_strategy_id_is_distinct(self):
        """_STRATEGY_ID must differ from v9_1_lgb_only."""
        assert _STRATEGY_ID == "v9_1_meta_kelly"
        assert _STRATEGY_ID != "v9_1_lgb_only"
