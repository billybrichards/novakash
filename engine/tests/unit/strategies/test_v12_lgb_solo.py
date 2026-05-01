"""Unit tests for v12_lgb_solo hook — pure-v12 LGB strategy.

Mirrors the structure of test_v12_lgb_combo and test_v9_lgb_only, but
exercises the v12-solo path: probability_lgb_v12 is swapped into the
probability_lgb slot before delegating to v9_ensemble's gate stack.

6 tests:
  1. test_skip_when_v12_unavailable
  2. test_trade_when_v12_present
  3. test_swaps_probability_lgb_with_v12 (surface restored after delegation)
  4. test_classifier_nulled_lgb_only_forced
  5. test_metadata_includes_both_v9_and_v12
  6. test_strategy_id_correct
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
    """Build a minimal FullDataSurface for testing v12_lgb_solo.

    Default surface is biased UP (delta_chainlink/delta_tiingo positive,
    poly_direction='UP') so probability_lgb_v12 > 0.5 leads to a
    direction-aligned UP trade through v9_ensemble.
    """
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713000000,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=84000.0,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.004, delta_source="tiingo_rest_candle",
        vpin=0.55, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.62, v2_probability_raw=0.60,
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
        poly_direction="UP", poly_trade_advised=True, poly_confidence=0.62,
        poly_confidence_distance=0.12, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="UP", v4_recommended_collateral_pct=0.025,
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


class TestV12LgbSolo:
    """Tests for evaluate_v12_lgb_solo hook."""

    def test_skip_when_v12_unavailable(self):
        """SKIP when probability_lgb_v12 is None — hook bails before
        delegating to the gate stack."""
        from strategies.configs.v12_lgb_solo import evaluate_v12_lgb_solo

        surface = _make_surface(
            probability_lgb=0.70,
            probability_lgb_v12=None,
        )
        decision = evaluate_v12_lgb_solo(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason is not None
        assert "probability_lgb_v12" in decision.skip_reason
        assert "unavailable" in decision.skip_reason
        assert decision.strategy_id == "v12_lgb_solo"
        assert decision.strategy_version == "12.0.0-solo"
        assert decision.metadata["probability_lgb_v12"] is None

    def test_trade_when_v12_present(self):
        """When probability_lgb_v12=0.80 and surface is configured for
        an UP trade, the hook delegates to v9_ensemble. Verify metadata
        flags are set correctly. (Whether the gate stack actually
        TRADEs vs SKIPs depends on many factors; we assert the v12-solo
        identity + metadata regardless.)"""
        from strategies.configs.v12_lgb_solo import evaluate_v12_lgb_solo

        surface = _make_surface(
            probability_lgb=0.55,
            probability_lgb_v12=0.80,    # strong UP, dist=0.30
        )
        decision = evaluate_v12_lgb_solo(surface)

        assert decision.strategy_id == "v12_lgb_solo"
        assert decision.strategy_version == "12.0.0-solo"
        assert decision.metadata["v12_solo_model"] is True
        assert decision.metadata["lgb_only_forced"] is True
        assert decision.metadata["probability_lgb_v12"] == 0.80
        # If the gate stack lets the trade through, direction must be UP
        # (driven by p_v12=0.80). If it SKIPs for unrelated gate reasons,
        # we still asserted the v12 identity above.
        if decision.action == "TRADE":
            assert decision.direction == "UP"

    def test_swaps_probability_lgb_with_v12(self):
        """Verify surface.probability_lgb is restored after delegation —
        the swap is transient and must not leak."""
        from strategies.configs.v12_lgb_solo import evaluate_v12_lgb_solo

        original_lgb = 0.55
        surface = _make_surface(
            probability_lgb=original_lgb,
            probability_lgb_v12=0.80,
        )
        evaluate_v12_lgb_solo(surface)

        # Surface must be restored to original value (no leak)
        assert surface.probability_lgb == original_lgb
        assert surface.probability_lgb_v12 == 0.80

    def test_classifier_nulled_lgb_only_forced(self):
        """probability_classifier should be nulled in the metadata and
        lgb_only_forced=True (same pattern as v9_lgb_only / v10_lgb_only)."""
        from strategies.configs.v12_lgb_solo import evaluate_v12_lgb_solo

        surface = _make_surface(
            probability_lgb=0.55,
            probability_lgb_v12=0.80,
            probability_classifier=0.65,  # gets nulled by hook
        )
        decision = evaluate_v12_lgb_solo(surface)

        assert decision.metadata.get("probability_classifier") is None
        assert decision.metadata.get("lgb_only_forced") is True
        # Surface should also have probability_classifier restored
        assert surface.probability_classifier == 0.65

    def test_metadata_includes_both_v9_and_v12(self):
        """Metadata must include both probability_lgb_v12 AND
        probability_lgb_prod (= original v9 prob before swap) so
        downstream filters / audits have full visibility."""
        from strategies.configs.v12_lgb_solo import evaluate_v12_lgb_solo

        surface = _make_surface(
            probability_lgb=0.62,         # v9 prod
            probability_lgb_v12=0.78,     # v12 model
        )
        decision = evaluate_v12_lgb_solo(surface)

        assert "probability_lgb_v12" in decision.metadata
        assert "probability_lgb_prod" in decision.metadata
        assert decision.metadata["probability_lgb_v12"] == 0.78
        assert decision.metadata["probability_lgb_prod"] == 0.62

    def test_strategy_id_correct(self):
        """Strategy identity must always be v12_lgb_solo / 12.0.0-solo
        regardless of action (TRADE or SKIP)."""
        from strategies.configs.v12_lgb_solo import evaluate_v12_lgb_solo

        # Case 1: SKIP path (v12 unavailable)
        surface = _make_surface(probability_lgb_v12=None)
        decision = evaluate_v12_lgb_solo(surface)
        assert decision.strategy_id == "v12_lgb_solo"
        assert decision.strategy_version == "12.0.0-solo"

        # Case 2: delegated path (v12 present)
        surface = _make_surface(
            probability_lgb=0.55,
            probability_lgb_v12=0.80,
        )
        decision = evaluate_v12_lgb_solo(surface)
        assert decision.strategy_id == "v12_lgb_solo"
        assert decision.strategy_version == "12.0.0-solo"
