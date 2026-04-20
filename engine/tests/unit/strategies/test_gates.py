"""Tests for individual gates -- each tested with FullDataSurface pass/fail cases."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from strategies.data_surface import FullDataSurface
from strategies.gates.timing import TimingGate
from strategies.gates.direction import DirectionGate
from strategies.gates.confidence import ConfidenceGate
from strategies.gates.session_hours import SessionHoursGate
from strategies.gates.trade_advised import TradeAdvisedGate
from strategies.gates.delta_magnitude import DeltaMagnitudeGate
from strategies.gates.source_agreement import SourceAgreementGate
from strategies.gates.taker_flow import TakerFlowGate
from strategies.gates.cg_confirmation import CGConfirmationGate
from strategies.gates.spread import SpreadGate
from strategies.gates.dynamic_cap import DynamicCapGate
from strategies.gates.regime import RegimeGate
from strategies.gates.macro_direction import MacroDirectionGate
from strategies.gates.clob_sizing import CLOBSizingGate


def _make_surface(**overrides) -> FullDataSurface:
    """Create a FullDataSurface with sensible defaults and overrides."""
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713000000,
        eval_offset=120, assembled_at=1713000000.0,
        current_price=84500.0, open_price=84000.0,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.004, delta_source="tiingo_rest_candle",
        vpin=0.45, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.62, v2_probability_raw=0.60,
        v2_quantiles_p10=-0.001, v2_quantiles_p50=0.002, v2_quantiles_p90=0.005,
        # Audit #121 Path 1 ensemble fields (default None — most tests don't care)
        probability_lgb=None, probability_classifier=None, ensemble_config=None,
        v3_5m_composite=0.6, v3_15m_composite=0.55, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None, v3_sub_momentum=None,
        v4_regime="calm_trend", v4_regime_confidence=0.85, v4_regime_persistence=0.9,
        v4_macro_bias="BULL", v4_macro_direction_gate="ALLOW_ALL", v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="UP", poly_trade_advised=True, poly_confidence=0.62,
        poly_confidence_distance=0.12, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="UP", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.52, clob_up_ask=0.54, clob_down_bid=0.46,
        clob_down_ask=0.48, clob_implied_up=0.53,
        gamma_up_price=0.55, gamma_down_price=0.45,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=1_200_000.0, cg_taker_sell_vol=800_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── TimingGate ──

class TestTimingGate:
    def test_pass_in_range(self):
        gate = TimingGate(90, 150)
        r = gate.evaluate(_make_surface(eval_offset=120))
        assert r.passed

    def test_fail_below_min(self):
        gate = TimingGate(90, 150)
        r = gate.evaluate(_make_surface(eval_offset=60))
        assert not r.passed

    def test_fail_above_max(self):
        gate = TimingGate(90, 150)
        r = gate.evaluate(_make_surface(eval_offset=180))
        assert not r.passed

    def test_boundary_min(self):
        gate = TimingGate(90, 150)
        assert gate.evaluate(_make_surface(eval_offset=90)).passed

    def test_boundary_max(self):
        gate = TimingGate(90, 150)
        assert gate.evaluate(_make_surface(eval_offset=150)).passed

    def test_none_offset_fails(self):
        gate = TimingGate(90, 150)
        assert not gate.evaluate(_make_surface(eval_offset=None)).passed


# ── DirectionGate ──

class TestDirectionGate:
    def test_up_matches_up(self):
        gate = DirectionGate("UP")
        assert gate.evaluate(_make_surface(poly_direction="UP")).passed

    def test_up_rejects_down(self):
        gate = DirectionGate("UP")
        assert not gate.evaluate(_make_surface(poly_direction="DOWN")).passed

    def test_any_always_passes(self):
        gate = DirectionGate("ANY")
        assert gate.evaluate(_make_surface(poly_direction="DOWN")).passed

    def test_fallback_to_v2_probability(self):
        gate = DirectionGate("DOWN")
        r = gate.evaluate(_make_surface(poly_direction=None, v2_probability_up=0.4))
        assert r.passed  # 0.4 < 0.5 -> DOWN

    def test_no_direction_fails(self):
        gate = DirectionGate("UP")
        r = gate.evaluate(_make_surface(poly_direction=None, v2_probability_up=None))
        assert not r.passed


# ── ConfidenceGate ──

class TestConfidenceGate:
    def test_pass_above_min(self):
        gate = ConfidenceGate(min_dist=0.10)
        assert gate.evaluate(_make_surface(poly_confidence_distance=0.12)).passed

    def test_fail_below_min(self):
        gate = ConfidenceGate(min_dist=0.10)
        assert not gate.evaluate(_make_surface(poly_confidence_distance=0.08)).passed

    def test_max_dist_pass(self):
        gate = ConfidenceGate(min_dist=0.10, max_dist=0.20)
        assert gate.evaluate(_make_surface(poly_confidence_distance=0.15)).passed

    def test_max_dist_fail(self):
        gate = ConfidenceGate(min_dist=0.10, max_dist=0.20)
        assert not gate.evaluate(_make_surface(poly_confidence_distance=0.25)).passed

    def test_fallback_to_v2(self):
        gate = ConfidenceGate(min_dist=0.10)
        r = gate.evaluate(_make_surface(poly_confidence_distance=None, v2_probability_up=0.62))
        assert r.passed  # |0.62 - 0.5| = 0.12 >= 0.10

    def test_no_data_fails(self):
        gate = ConfidenceGate(min_dist=0.10)
        r = gate.evaluate(_make_surface(poly_confidence_distance=None, v2_probability_up=None))
        assert not r.passed


# ── SessionHoursGate ──

class TestSessionHoursGate:
    def test_pass_in_set(self):
        gate = SessionHoursGate([23, 0, 1, 2])
        assert gate.evaluate(_make_surface(hour_utc=0)).passed

    def test_fail_outside_set(self):
        gate = SessionHoursGate([23, 0, 1, 2])
        assert not gate.evaluate(_make_surface(hour_utc=12)).passed

    def test_none_fails(self):
        gate = SessionHoursGate([23, 0, 1, 2])
        assert not gate.evaluate(_make_surface(hour_utc=None)).passed


# ── TradeAdvisedGate ──

class TestTradeAdvisedGate:
    def test_pass_when_true(self):
        gate = TradeAdvisedGate()
        assert gate.evaluate(_make_surface(poly_trade_advised=True)).passed

    def test_fail_when_false(self):
        gate = TradeAdvisedGate()
        assert not gate.evaluate(_make_surface(poly_trade_advised=False)).passed

    def test_fail_when_none(self):
        gate = TradeAdvisedGate()
        assert not gate.evaluate(_make_surface(poly_trade_advised=None)).passed


# ── DeltaMagnitudeGate ──

class TestDeltaMagnitudeGate:
    def test_pass(self):
        gate = DeltaMagnitudeGate(min_threshold=0.0005)
        assert gate.evaluate(_make_surface(delta_pct=0.004)).passed

    def test_fail(self):
        gate = DeltaMagnitudeGate(min_threshold=0.0005)
        assert not gate.evaluate(_make_surface(delta_pct=0.0001)).passed

    def test_negative_delta_uses_abs(self):
        gate = DeltaMagnitudeGate(min_threshold=0.0005)
        assert gate.evaluate(_make_surface(delta_pct=-0.001)).passed


# ── SourceAgreementGate ──

class TestSourceAgreementGate:
    def test_pass_all_agree_up(self):
        gate = SourceAgreementGate(min_sources=2)
        r = gate.evaluate(_make_surface(delta_tiingo=0.01, delta_chainlink=0.01, delta_binance=0.01))
        assert r.passed

    def test_pass_two_agree(self):
        gate = SourceAgreementGate(min_sources=2)
        r = gate.evaluate(_make_surface(delta_tiingo=0.01, delta_chainlink=0.01, delta_binance=-0.01))
        assert r.passed

    def test_fail_not_enough_sources(self):
        gate = SourceAgreementGate(min_sources=2)
        r = gate.evaluate(_make_surface(delta_tiingo=0.01, delta_chainlink=None, delta_binance=None))
        assert not r.passed


# ── TakerFlowGate ──

class TestTakerFlowGate:
    def test_up_with_buy_dominant(self):
        gate = TakerFlowGate()
        r = gate.evaluate(_make_surface(
            poly_direction="UP", cg_taker_buy_vol=1200, cg_taker_sell_vol=800
        ))
        assert r.passed

    def test_up_with_sell_dominant_fails(self):
        gate = TakerFlowGate()
        r = gate.evaluate(_make_surface(
            poly_direction="UP", cg_taker_buy_vol=800, cg_taker_sell_vol=1200
        ))
        assert not r.passed

    def test_no_cg_data_passes(self):
        gate = TakerFlowGate()
        r = gate.evaluate(_make_surface(cg_taker_buy_vol=None, cg_taker_sell_vol=None))
        assert r.passed


# ── CGConfirmationGate ──

class TestCGConfirmationGate:
    def test_pass_normal(self):
        gate = CGConfirmationGate(liq_threshold=1_000_000)
        assert gate.evaluate(_make_surface(cg_liq_total=500_000)).passed

    def test_fail_cascade(self):
        gate = CGConfirmationGate(liq_threshold=1_000_000)
        assert not gate.evaluate(_make_surface(cg_liq_total=2_000_000)).passed

    def test_no_cg_passes(self):
        gate = CGConfirmationGate()
        assert gate.evaluate(_make_surface(cg_oi_usd=None)).passed


# ── SpreadGate ──

class TestSpreadGate:
    def test_pass_narrow_spread(self):
        gate = SpreadGate(max_spread_bps=500)
        r = gate.evaluate(_make_surface(
            poly_direction="UP", clob_up_bid=0.52, clob_up_ask=0.54
        ))
        assert r.passed

    def test_fail_wide_spread(self):
        gate = SpreadGate(max_spread_bps=100)
        r = gate.evaluate(_make_surface(
            poly_direction="UP", clob_up_bid=0.40, clob_up_ask=0.60
        ))
        assert not r.passed

    def test_no_clob_passes(self):
        gate = SpreadGate(max_spread_bps=100)
        r = gate.evaluate(_make_surface(clob_up_bid=None, clob_up_ask=None))
        assert r.passed


# ── DynamicCapGate ──

class TestDynamicCapGate:
    def test_sets_cap(self):
        gate = DynamicCapGate(default_cap=0.65)
        r = gate.evaluate(_make_surface(poly_confidence_distance=0.15))
        assert r.passed
        assert "entry_cap" in r.data

    def test_default_cap_when_no_data(self):
        gate = DynamicCapGate(default_cap=0.65)
        r = gate.evaluate(_make_surface(
            poly_confidence_distance=None, v2_probability_up=None
        ))
        assert r.data["entry_cap"] == 0.65


# ── RegimeGate ──

class TestRegimeGate:
    def test_pass_allowed(self):
        gate = RegimeGate(allowed=["calm_trend", "volatile_trend"])
        assert gate.evaluate(_make_surface(v4_regime="calm_trend")).passed

    def test_fail_not_allowed(self):
        gate = RegimeGate(allowed=["calm_trend", "volatile_trend"])
        assert not gate.evaluate(_make_surface(v4_regime="chop")).passed

    def test_no_regime_passes(self):
        gate = RegimeGate(allowed=["calm_trend"])
        assert gate.evaluate(_make_surface(v4_regime=None)).passed


# ── MacroDirectionGate ──

class TestMacroDirectionGate:
    def test_allow_all_passes(self):
        gate = MacroDirectionGate()
        assert gate.evaluate(_make_surface(v4_macro_direction_gate="ALLOW_ALL")).passed

    def test_long_only_blocks_down(self):
        gate = MacroDirectionGate()
        assert not gate.evaluate(_make_surface(
            v4_macro_direction_gate="LONG_ONLY", poly_direction="DOWN"
        )).passed

    def test_long_only_allows_up(self):
        gate = MacroDirectionGate()
        assert gate.evaluate(_make_surface(
            v4_macro_direction_gate="LONG_ONLY", poly_direction="UP"
        )).passed


# ── CLOBSizingGate ──

class TestCLOBSizingGate:
    def _make_gate(self):
        return CLOBSizingGate(
            schedule=[
                {"threshold": 0.55, "modifier": 2.0, "label": "strong"},
                {"threshold": 0.35, "modifier": 1.2, "label": "mild"},
                {"threshold": 0.25, "modifier": 1.0, "label": "contrarian"},
                {"threshold": 0.0, "modifier": 0.0, "label": "skip"},
            ],
            null_modifier=1.5,
        )

    def test_strong_band(self):
        gate = self._make_gate()
        r = gate.evaluate(_make_surface(clob_down_ask=0.60))
        assert r.passed
        assert r.data["size_modifier"] == 2.0

    def test_mild_band(self):
        gate = self._make_gate()
        r = gate.evaluate(_make_surface(clob_down_ask=0.40))
        assert r.passed
        assert r.data["size_modifier"] == 1.2

    def test_skip_band(self):
        gate = self._make_gate()
        r = gate.evaluate(_make_surface(clob_down_ask=0.10))
        assert not r.passed  # modifier=0.0 means skip

    def test_null_clob(self):
        gate = self._make_gate()
        r = gate.evaluate(_make_surface(clob_down_ask=None))
        assert r.passed
        assert r.data["size_modifier"] == 1.5
