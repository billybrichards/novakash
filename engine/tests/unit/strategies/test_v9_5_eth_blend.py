"""Unit tests for v9_5_eth_blend GHOST strategy.

Coverage:
- model-not-loaded SKIP (v9_5_eth_model_not_loaded) — forward-compat path
- defensive asset guard (SKIP on non-ETH surface — wrong_asset)
- eval_offset outside band [60, 240] (outside_eval_band)
- conviction_below_threshold SKIP
- TRADE path: UP at 0.96, DOWN at 0.04 per RDS notes #579/#584
- gate_params runtime override respected
- min_consecutive_pass_ticks=2 default — first tick SKIPs, second TRADEs
- Metadata shape on TRADE includes probability_lgb_v9_5_eth + thresholds
- Reads from probability_lgb_v9_5_eth, NOT probability_lgb_v9_2_eth
- Strategy is asset-restricted: same threshold passes for BTC surface SKIPs
- Co-exists with v9_2_eth_raw_lgb: independent consec-tick state

Test strategy: synthetic _make_surface() fixture; uses _gp.set_active /
_gp.reset_active (same pattern as test_v9_2_eth_raw_lgb.py).
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.configs.v9_5_eth_blend import evaluate_v9_5_eth_blend
from strategies import gate_params as _gp
from strategies.data_surface import FullDataSurface


# ── Surface factory ────────────────────────────────────────────────────────

def _make_surface(**overrides) -> FullDataSurface:
    """Default surface for ETH v9.5 strategy tests.

    asset=ETH, eval_offset=150 (in band [60, 240]), hour_utc=12,
    probability_lgb_v9_5_eth=0.97 (above the 0.96 UP threshold).
    """
    defaults = dict(
        asset="ETH", timescale="5m",
        window_ts=1713009600,
        eval_offset=150, assembled_at=time.time(),
        current_price=3400.0, open_price=3380.0,
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
        hour_utc=12, seconds_to_close=120,
        probability_lgb_v9_5_eth=0.97,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ── Gate-params helpers ────────────────────────────────────────────────────

_BASE_PARAMS: dict[str, Any] = {
    # v1.0.0 thresholds per RDS notes #579 dedup analysis + #584 final
    # operating point: tight-corner UP=0.96, DOWN=0.04, wider eval_offset
    # band than v9.2_eth (60-240 vs 120-210), and 2 consecutive ticks
    # instead of 1.
    "up_threshold": 0.96,
    "down_threshold": 0.04,
    "eval_offset_min": 60,
    "eval_offset_max": 240,
    # NOTE: tests override min_consecutive_pass_ticks=1 by default to keep
    # threshold/eval-band tests focused. A dedicated TestConsecTicks class
    # exercises the default 2-tick gate explicitly.
    "min_consecutive_pass_ticks": 1,
    "expected_asset": "ETH",
    "entry_cap": 0.96,
    "collateral_pct": 0.025,
    "gtc_cap": 0.96,
}


@contextmanager
def _params(**extra):
    """Context manager that activates gate_params for the duration of a test."""
    p = dict(_BASE_PARAMS)
    p.update(extra)
    token = _gp.set_active(p)
    try:
        yield
    finally:
        _gp.reset_active(token)


@pytest.fixture(autouse=True)
def _clear_consec_state():
    """Reset consecutive-tick state and gate_params before/after each test."""
    from strategies.configs import v9_5_eth_blend
    v9_5_eth_blend._consec_state.clear()
    yield
    v9_5_eth_blend._consec_state.clear()


# ── Forward-compat: timesfm side not emitting yet ──────────────────────────

class TestForwardCompat:
    def test_model_not_loaded_skips_cleanly(self):
        """When the snapshot has no probability_lgb_v9_5_eth, the strategy
        SKIPs with a clear reason — no crash."""
        surface = _make_surface(probability_lgb_v9_5_eth=None)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_eth_model_not_loaded"
        # Decision still includes the strategy id even on SKIP.
        assert d.strategy_id == "v9_5_eth_blend"

    def test_strategy_does_NOT_read_v9_2_eth_field(self):
        """v9.5 strategy must read probability_lgb_v9_5_eth, NOT the
        v9.2 ETH field. If only the v9.2 ETH field is populated, the v9.5
        strategy SKIPs (forward-compat with v9.2 deployed but v9.5 not yet
        emitting)."""
        surface = _make_surface(
            probability_lgb_v9_5_eth=None,
            probability_lgb_v9_2_eth=0.99,  # v9.2 high but v9.5 missing
        )
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "v9_5_eth_model_not_loaded"


# ── Defensive asset guard ──────────────────────────────────────────────────

class TestAssetGuard:
    def test_btc_surface_skips_even_with_high_prob(self):
        """v9.5 ETH strategy refuses to fire on a BTC surface even at high
        conviction — defensive guard since the model is ETH-only."""
        surface = _make_surface(asset="BTC", probability_lgb_v9_5_eth=0.97)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"

    def test_xrp_surface_skips(self):
        surface = _make_surface(asset="XRP", probability_lgb_v9_5_eth=0.97)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "wrong_asset"


# ── Eval-offset band ───────────────────────────────────────────────────────

class TestEvalOffsetBand:
    def test_below_min_skips(self):
        """eval_offset=50 is below the band min of 60."""
        surface = _make_surface(eval_offset=50)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")

    def test_above_max_skips(self):
        """eval_offset=250 is above the band max of 240."""
        surface = _make_surface(eval_offset=250)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")

    def test_at_min_60_fires(self):
        surface = _make_surface(eval_offset=60, probability_lgb_v9_5_eth=0.97)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_at_max_240_fires(self):
        surface = _make_surface(eval_offset=240, probability_lgb_v9_5_eth=0.97)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_none_eval_offset_skips(self):
        surface = _make_surface(eval_offset=None)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")


# ── Threshold gating ───────────────────────────────────────────────────────

class TestThresholds:
    def test_up_fires_at_0_96(self):
        """UP threshold is 0.96 per RDS notes #584 dedup analysis."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.96)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"
        assert d.strategy_id == "v9_5_eth_blend"
        assert d.entry_reason == "v9_5_eth_blend_pass"

    def test_up_fires_above_0_96(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.99)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_down_fires_at_0_04(self):
        """DOWN threshold is 0.04 per RDS notes #584 dedup analysis."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.04)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_down_fires_below_0_04(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.02)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "DOWN"

    def test_between_thresholds_skips(self):
        # 0.50 is neither >= 0.96 nor <= 0.04
        surface = _make_surface(probability_lgb_v9_5_eth=0.50)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_just_below_up_skips(self):
        # 0.95 < 0.96 — below UP threshold
        surface = _make_surface(probability_lgb_v9_5_eth=0.95)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"

    def test_just_above_down_skips(self):
        # 0.05 > 0.04 — above DOWN threshold
        surface = _make_surface(probability_lgb_v9_5_eth=0.05)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason == "conviction_below_threshold"


# ── Consecutive-tick gate ──────────────────────────────────────────────────

class TestConsecTicks:
    def test_default_two_tick_gate(self):
        """Default min_consecutive_pass_ticks=2 — first qualifying tick must
        SKIP awaiting_consec_ticks; second consecutive tick TRADEs."""
        # Use the strategy default (2) — don't override via _params.
        surface = _make_surface(probability_lgb_v9_5_eth=0.97)
        # Activate _BASE_PARAMS WITHOUT min_consecutive_pass_ticks override
        # so the strategy uses its own default of 2.
        params = {k: v for k, v in _BASE_PARAMS.items()
                  if k != "min_consecutive_pass_ticks"}
        token = _gp.set_active(params)
        try:
            d1 = evaluate_v9_5_eth_blend(surface)
            assert d1.action == "SKIP"
            assert d1.skip_reason == "awaiting_consec_ticks"
            assert d1.metadata["consec_tick_count"] == 1
            assert d1.metadata["min_consecutive_pass_ticks"] == 2
            d2 = evaluate_v9_5_eth_blend(surface)
            assert d2.action == "TRADE"
            assert d2.direction == "UP"
            assert d2.metadata["consec_tick_count"] == 2
        finally:
            _gp.reset_active(token)


# ── Runtime override ───────────────────────────────────────────────────────

class TestRuntimeOverride:
    def test_runtime_override_lowers_up_threshold(self):
        """gate_params runtime override changes thresholds."""
        surface = _make_surface(probability_lgb_v9_5_eth=0.90)
        # with default 0.96 threshold: SKIP
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        # with overridden 0.85 threshold: TRADE
        with _params(up_threshold=0.85):
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        assert d.direction == "UP"

    def test_runtime_override_narrows_eval_band(self):
        # surface at eval_offset=80 fires under default [60, 240]
        surface = _make_surface(eval_offset=80, probability_lgb_v9_5_eth=0.97)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        # narrow to [120, 240] — 80 now out of band
        with _params(eval_offset_min=120):
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "SKIP"
        assert d.skip_reason.startswith("outside_eval_band")


# ── Metadata shape ─────────────────────────────────────────────────────────

class TestMetadata:
    def test_metadata_contains_probability_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.97)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        assert "probability_lgb_v9_5_eth" in d.metadata
        assert d.metadata["probability_lgb_v9_5_eth"] == pytest.approx(0.97)
        assert d.metadata["up_threshold"] == pytest.approx(0.96)
        assert d.metadata["down_threshold"] == pytest.approx(0.04)
        assert d.metadata["eval_offset_min"] == 60
        assert d.metadata["eval_offset_max"] == 240
        assert d.metadata["asset"] == "ETH"
        # Sanity — the v9.2 ETH field is NOT in v9.5 metadata
        assert "probability_lgb_v9_2_eth" not in d.metadata

    def test_metadata_contains_sizing_on_trade(self):
        surface = _make_surface(probability_lgb_v9_5_eth=0.97)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        assert d.metadata["entry_cap"] == pytest.approx(0.96)
        assert d.metadata["collateral_pct"] == pytest.approx(0.025)
        assert d.metadata["gtc_cap"] == pytest.approx(0.96)

    def test_confidence_score_is_high_at_0_97(self):
        # |0.97 - 0.5| * 2 = 0.94 >= 0.40 -> HIGH
        surface = _make_surface(probability_lgb_v9_5_eth=0.97)
        with _params():
            d = evaluate_v9_5_eth_blend(surface)
        assert d.action == "TRADE"
        assert d.confidence == "HIGH"
        assert d.confidence_score == pytest.approx(0.94)


# ── Co-existence with v9_2_eth_raw_lgb ──────────────────────────────────────

class TestCoexistenceWithV9_2:
    def test_independent_consec_tick_state(self):
        """The v9.5 strategy's _consec_state dict is module-local, so it must
        not collide with v9_2_eth_raw_lgb's state. Verified by importing
        both modules and asserting their _consec_state objects are distinct."""
        from strategies.configs import v9_5_eth_blend as v95
        from strategies.configs import v9_2_eth_raw_lgb as v92
        # Different module-level dict objects (id check).
        assert v95._consec_state is not v92._consec_state

    def test_default_yaml_status_is_ghost(self):
        """YAML config must default GHOST per feedback_no_auto_promote.md."""
        from pathlib import Path
        import yaml

        yaml_path = (
            Path(__file__).resolve().parents[3]
            / "strategies"
            / "configs"
            / "v9_5_eth_blend.yaml"
        )
        cfg = yaml.safe_load(yaml_path.read_text())
        assert cfg["status"] == "GHOST"
        assert cfg["mode"] == "GHOST"
        assert cfg["asset"] == "ETH"
        # max_position_usd cap = $5 for initial shadow window
        assert cfg["risk"]["max_position_usd"] == 5
        # Tight-corner thresholds per RDS #584
        assert cfg["gate_params"]["up_threshold"] == 0.96
        assert cfg["gate_params"]["down_threshold"] == 0.04
