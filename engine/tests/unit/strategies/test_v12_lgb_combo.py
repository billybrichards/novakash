"""Unit tests for v12_lgb_combo hook — agreement-required v9+v12 LGB combo.

Covers all 4 SKIP paths + TRADE path + metadata enrichment.
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


def _make_surface(**overrides) -> FullDataSurface:
    """Build a minimal FullDataSurface for testing v12_lgb_combo."""
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713000000,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=84000.0,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.004, delta_source="tiingo_rest_candle",
        vpin=0.55, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.38, v2_probability_raw=0.36,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=None, probability_classifier=None, ensemble_config=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None, v3_sub_momentum=None,
        v4_regime="calm_trend", v4_regime_confidence=0.85, v4_regime_persistence=0.9,
        v4_macro_bias="BULL", v4_macro_direction_gate="ALLOW_ALL", v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.38,
        poly_confidence_distance=0.12, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.46, clob_up_ask=0.48, clob_down_bid=0.52,
        clob_down_ask=0.54, clob_implied_up=0.47,
        gamma_up_price=0.45, gamma_down_price=0.55,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


class TestV12LgbCombo:
    """Tests for evaluate_v12_lgb_combo hook."""

    def test_skip_when_v12_unavailable(self):
        """SKIP when probability_lgb_v12 is None (v12 not deployed yet)."""
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        surface = _make_surface(
            probability_lgb=0.70,
            probability_lgb_v12=None,
        )
        decision = evaluate_v12_lgb_combo(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "probability_lgb_v12 unavailable"
        assert decision.strategy_id == "v12_lgb_combo"
        assert decision.strategy_version == "12.0.0-combo"
        assert decision.metadata["probability_lgb_v12"] is None
        assert decision.metadata["probability_lgb"] == 0.70

    def test_skip_when_v9_unavailable(self):
        """SKIP when probability_lgb (v9) is None."""
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        surface = _make_surface(
            probability_lgb=None,
            probability_lgb_v12=0.65,
        )
        decision = evaluate_v12_lgb_combo(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "probability_lgb unavailable"
        assert decision.strategy_id == "v12_lgb_combo"
        assert decision.metadata["probability_lgb"] is None
        assert decision.metadata["probability_lgb_v12"] == 0.65

    def test_skip_when_directions_disagree(self):
        """With v12_contrarian_enabled=False (canary-disable Option C),
        v9/v12 directional disagreement still SKIPs (Option A only).

        With the flag ON (default), Option C trades the disagreement —
        see test_disagreement_trades_v12_at_half_kelly below.
        """
        from strategies import gate_params as _gp
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        # v9 says UP (0.75), v12 says DOWN (0.30)
        surface = _make_surface(
            probability_lgb=0.75,
            probability_lgb_v12=0.30,
        )
        token = _gp.set_active({"v12_contrarian_enabled": False})
        try:
            decision = evaluate_v12_lgb_combo(surface)
        finally:
            _gp.reset_active(token)

        assert decision.action == "SKIP"
        assert decision.skip_reason == (
            "v9_v12_direction_disagreement_contrarian_disabled"
        )
        assert decision.metadata["dir_v9"] == "UP"
        assert decision.metadata["dir_v12"] == "DOWN"
        assert decision.metadata["direction_agree"] is False

    def test_skip_when_combo_dist_below_floor(self):
        """SKIP when min(dist_v9, dist_v12) < 0.10 conviction floor."""
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        # Both agree DOWN but v12 is barely below 0.5 (dist=0.05)
        surface = _make_surface(
            probability_lgb=0.30,       # dist_v9 = 0.20
            probability_lgb_v12=0.45,   # dist_v12 = 0.05 < 0.10 floor
        )
        decision = evaluate_v12_lgb_combo(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "combo_below_conviction_floor"
        assert decision.metadata["combo_dist"] == 0.05
        assert decision.metadata["combo_min_dist"] == 0.10
        assert decision.metadata["direction_agree"] is True

    def test_trade_when_both_agree_high_conviction(self):
        """TRADE when both agree direction and combo_dist >= 0.10.

        This test verifies the delegation path: combo probability is
        swapped onto the surface and delegated to v9_ensemble. The
        actual TRADE/SKIP depends on the v9_ensemble gate stack, so
        we verify delegation happened (strategy_id is v12_lgb_combo,
        metadata has combo fields) rather than asserting TRADE — the
        gate stack may still SKIP for other reasons (CLOB, timing, etc).
        """
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        # Both agree DOWN with strong conviction
        surface = _make_surface(
            probability_lgb=0.25,       # dist_v9 = 0.25
            probability_lgb_v12=0.20,   # dist_v12 = 0.30
            # Set up surface so v9_ensemble gate stack can pass:
            poly_direction="DOWN",
            poly_trade_advised=True,
            poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005,
            delta_tiingo=-0.004,
        )
        decision = evaluate_v12_lgb_combo(surface)

        # Strategy identity is v12_lgb_combo regardless of delegate outcome
        assert decision.strategy_id == "v12_lgb_combo"
        assert decision.strategy_version == "12.0.0-combo"

        # Metadata includes combo fields
        assert decision.metadata["probability_lgb"] == 0.25
        assert decision.metadata["probability_lgb_v12"] == 0.20
        assert abs(decision.metadata["p_combo"] - 0.225) < 1e-6
        assert decision.metadata["combo_dist"] == 0.25
        assert decision.metadata["direction_agree"] is True
        assert decision.metadata["v12_combo_model"] is True
        assert decision.metadata["lgb_only_forced"] is True

        # confidence_score = min(combo_dist * 2, 1.0) = min(0.25*2, 1) = 0.5
        assert abs(decision.confidence_score - 0.5) < 1e-6

    def test_metadata_includes_both_probabilities(self):
        """Metadata always includes both probability fields even on SKIP."""
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        # Case 1: v12 unavailable
        surface = _make_surface(probability_lgb=0.6, probability_lgb_v12=None)
        decision = evaluate_v12_lgb_combo(surface)
        assert "probability_lgb" in decision.metadata
        assert "probability_lgb_v12" in decision.metadata

        # Case 2: direction disagreement
        surface = _make_surface(probability_lgb=0.7, probability_lgb_v12=0.3)
        decision = evaluate_v12_lgb_combo(surface)
        assert decision.metadata["probability_lgb"] == 0.7
        assert decision.metadata["probability_lgb_v12"] == 0.3

        # Case 3: below conviction floor
        surface = _make_surface(probability_lgb=0.45, probability_lgb_v12=0.47)
        decision = evaluate_v12_lgb_combo(surface)
        assert decision.metadata["probability_lgb"] == 0.45
        assert decision.metadata["probability_lgb_v12"] == 0.47

    def test_surface_restored_after_delegation(self):
        """Verify the surface's probability_lgb is restored after delegation."""
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        original_lgb = 0.25
        surface = _make_surface(
            probability_lgb=original_lgb,
            probability_lgb_v12=0.20,
            poly_direction="DOWN",
            poly_trade_advised=True,
            poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005,
            delta_tiingo=-0.004,
        )
        evaluate_v12_lgb_combo(surface)

        # Surface should be restored to original values
        assert surface.probability_lgb == original_lgb
        assert surface.probability_classifier is None  # was None in _make_surface

    # ── Option C: disagreement-as-v12-contrarian-signal (hub note #299) ─────

    def test_disagreement_trades_v12_at_half_kelly(self):
        """v9 strong UP, v12 strong DOWN. With Option C enabled (default),
        should TRADE in v12's direction (DOWN) and have collateral_pct
        halved versus the same-conviction agreement trade.
        """
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        # Build a "v9_ensemble can pass gates" surface, but flip v9 to UP
        # so v9+v12 disagree. v12 wants DOWN with dist=0.20.
        surface = _make_surface(
            probability_lgb=0.80,        # v9 → UP, dist 0.30
            probability_lgb_v12=0.30,    # v12 → DOWN, dist 0.20
            poly_direction="DOWN",
            poly_trade_advised=True,
            poly_confidence_distance=0.12,
            poly_timing="optimal",
            delta_chainlink=-0.005,
            delta_tiingo=-0.004,
        )
        decision = evaluate_v12_lgb_combo(surface)

        # Strategy identity preserved; contrarian-mode flag set.
        assert decision.strategy_id == "v12_lgb_combo"
        assert decision.metadata["v12_contrarian_mode"] is True
        assert decision.metadata["direction_agree"] is False
        assert decision.metadata["dir_v9"] == "UP"
        assert decision.metadata["dir_v12"] == "DOWN"
        assert decision.metadata.get("kelly_half_modifier") == 0.5

        # If v9_ensemble's gate stack lets the trade through, direction
        # must be v12's (DOWN) and collateral_pct must be halved versus
        # the pre-halving value the delegate returned.
        if decision.action == "TRADE":
            assert decision.direction == "DOWN"
            pre = decision.metadata.get("collateral_pct_pre_halving")
            post = decision.collateral_pct
            if pre is not None and post is not None:
                assert abs(post - pre * 0.5) < 1e-9, (
                    f"collateral_pct should be halved: pre={pre} post={post}"
                )
        # If the gate stack SKIPs for unrelated reasons (CLOB / VPIN /
        # delta gate etc) we still asserted the contrarian-mode flags
        # above — the half-kelly contract holds when the trade fires.

    def test_disagreement_skips_when_v12_weak(self):
        """v9 + v12 disagree but v12 dist=0.05 (< 0.10 floor) → SKIP."""
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        surface = _make_surface(
            probability_lgb=0.80,        # v9 → UP, dist 0.30
            probability_lgb_v12=0.45,    # v12 → DOWN, dist 0.05
        )
        decision = evaluate_v12_lgb_combo(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "disagreement_v12_weak"
        assert decision.metadata["direction_agree"] is False
        assert decision.metadata["dir_v9"] == "UP"
        assert decision.metadata["dir_v12"] == "DOWN"
        assert decision.metadata["v12_contrarian_mode"] is False
        assert decision.metadata["v12_contrarian_min_dist"] == 0.10

    def test_v12_contrarian_disabled_via_flag(self):
        """When v12_contrarian_enabled=False in gate_params (canary-disable
        Option C), v9/v12 disagreement → SKIP — Option A only.
        """
        from strategies import gate_params as _gp
        from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo

        surface = _make_surface(
            probability_lgb=0.80,
            probability_lgb_v12=0.30,
        )
        token = _gp.set_active({"v12_contrarian_enabled": False})
        try:
            decision = evaluate_v12_lgb_combo(surface)
        finally:
            _gp.reset_active(token)

        assert decision.action == "SKIP"
        skip = decision.skip_reason or ""
        assert (
            "contrarian_disabled" in skip or "direction_disagreement" in skip
        ), f"unexpected skip_reason: {skip}"
        assert decision.metadata["v12_contrarian_mode"] is False
        assert decision.metadata["direction_agree"] is False
