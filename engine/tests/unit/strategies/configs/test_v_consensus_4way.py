"""Unit tests for v_consensus_4way entry_cap fix (PR fix/v-consensus-4way-entry-cap).

Root cause: entry_cap=0.0 on TRADE path caused fak_ladder_executor to submit
GTC orders at ~$0.01 price (round(0.0 + pi_bonus, 2) ≈ $0.01), which never
fill against a market trading at 0.40-0.80.

Fix: TRADE path reads fill_band_max from gate_params (default 0.82).
     SKIP paths retain entry_cap=0.0 (irrelevant for SKIP).

Coverage:
  1. test_trade_returns_entry_cap_from_fill_band_max     -- runtime override honoured
  2. test_trade_returns_default_entry_cap_when_no_override -- default 0.82 when no override
  3. test_skip_paths_entry_cap_zero_missing_signals      -- SKIP (models not loaded): entry_cap=0.0
  4. test_skip_paths_entry_cap_zero_disagreement         -- SKIP (no consensus): entry_cap=0.0
  5. test_trade_entry_cap_positive                       -- sanity: entry_cap > 0 on TRADE
  6. test_trade_direction_up                             -- UP consensus fires correctly
  7. test_trade_direction_down                           -- DOWN consensus fires correctly
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
from strategies.configs.v_consensus_4way import evaluate_consensus_4way

# ── Fixtures ──────────────────────────────────────────────────────────────────

_TS = 1746716400  # 2026-05-08 15:00:00 UTC
_OFFSET = 80      # 80s remaining — inside typical v_consensus_4way gate


def _make_surface(**overrides) -> FullDataSurface:
    """Build a minimal FullDataSurface that passes 4-way consensus (all UP)."""
    defaults = dict(
        asset="BTC", timescale="5m",
        window_ts=_TS,
        eval_offset=_OFFSET,
        assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=0.005, delta_tiingo=0.005, delta_chainlink=0.005,
        delta_pct=0.005, delta_source="chainlink",
        vpin=0.45, regime="volatile_trend", twap_delta=0.003,
        # 4-way consensus signals — all above 0.55 → UP consensus
        v2_probability_up=0.65,
        probability_lgb_v9_1=0.60,
        probability_lgb_v12=0.62,
        probability_classifier=0.58,
        # Required surface fields (not used by this hook)
        v2_probability_raw=0.65,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=0.62,
        ensemble_config=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None, v3_sub_momentum=None,
        v4_regime="volatile_trend", v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BULL", v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="UP", poly_trade_advised=True, poly_confidence=0.65,
        poly_confidence_distance=0.15, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="UP", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.62, clob_up_ask=0.65, clob_down_bid=0.35,
        clob_down_ask=0.38, clob_implied_up=0.63,
        gamma_up_price=0.63, gamma_down_price=0.37,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=1_200_000.0, cg_taker_sell_vol=800_000.0,
        cg_liq_total=200_000.0, cg_liq_long=100_000.0,
        cg_liq_short=100_000.0, cg_long_short_ratio=1.3,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=15, seconds_to_close=_OFFSET,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _make_down_surface(**overrides) -> FullDataSurface:
    """Surface with all 4 signals below 0.45 → DOWN consensus."""
    return _make_surface(
        v2_probability_up=0.35,
        probability_lgb_v9_1=0.38,
        probability_lgb_v12=0.40,
        probability_classifier=0.36,
        **overrides,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestEntryCap:
    """entry_cap on TRADE path must reflect fill_band_max, not 0.0."""

    def test_trade_returns_entry_cap_from_fill_band_max(self):
        """Runtime override fill_band_max=0.75 is used as entry_cap on TRADE."""
        surface = _make_surface()
        token = _gp.set_active({"fill_band_max": 0.75})
        try:
            decision = evaluate_consensus_4way(surface)
        finally:
            _gp._ACTIVE.reset(token)

        assert decision.action == "TRADE"
        assert decision.entry_cap == pytest.approx(0.75)

    def test_trade_returns_default_entry_cap_when_no_override(self):
        """No runtime override → entry_cap defaults to 0.82."""
        surface = _make_surface()
        token = _gp.set_active({})  # empty params — no fill_band_max set
        try:
            decision = evaluate_consensus_4way(surface)
        finally:
            _gp._ACTIVE.reset(token)

        assert decision.action == "TRADE"
        assert decision.entry_cap == pytest.approx(0.82)

    def test_trade_entry_cap_positive(self):
        """Sanity check: entry_cap > 0 so GTC orders never land at $0.01."""
        surface = _make_surface()
        token = _gp.set_active({})
        try:
            decision = evaluate_consensus_4way(surface)
        finally:
            _gp._ACTIVE.reset(token)

        assert decision.action == "TRADE"
        assert decision.entry_cap > 0.0, (
            f"entry_cap={decision.entry_cap} would cause GTC at ~$0.01"
        )

    def test_skip_paths_entry_cap_zero_missing_signals(self):
        """SKIP (models not loaded): entry_cap=0.0 — no regression."""
        surface = _make_surface(
            v2_probability_up=None,  # missing signal triggers graceful degradation
        )
        token = _gp.set_active({"fill_band_max": 0.75})
        try:
            decision = evaluate_consensus_4way(surface)
        finally:
            _gp._ACTIVE.reset(token)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "models_not_loaded"
        assert decision.entry_cap == 0.0  # SKIP path unchanged

    def test_skip_paths_entry_cap_zero_disagreement(self):
        """SKIP (consensus fails): entry_cap=0.0 — no regression."""
        # Mix UP and DOWN signals so consensus fails
        surface = _make_surface(
            v2_probability_up=0.65,   # UP
            probability_lgb_v9_1=0.30,  # DOWN — breaks consensus
            probability_lgb_v12=0.65,
            probability_classifier=0.65,
        )
        token = _gp.set_active({"fill_band_max": 0.75})
        try:
            decision = evaluate_consensus_4way(surface)
        finally:
            _gp._ACTIVE.reset(token)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "consensus_disagreement"
        assert decision.entry_cap == 0.0  # SKIP path unchanged


class TestConsensusDirections:
    """UP and DOWN consensus both produce TRADE with correct direction."""

    def test_trade_direction_up(self):
        """All 4 signals >= 0.55 → TRADE UP."""
        surface = _make_surface()
        token = _gp.set_active({})
        try:
            decision = evaluate_consensus_4way(surface)
        finally:
            _gp._ACTIVE.reset(token)

        assert decision.action == "TRADE"
        assert decision.direction == "UP"

    def test_trade_direction_down(self):
        """All 4 signals <= 0.45 → TRADE DOWN."""
        surface = _make_down_surface()
        token = _gp.set_active({})
        try:
            decision = evaluate_consensus_4way(surface)
        finally:
            _gp._ACTIVE.reset(token)

        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"

    def test_down_trade_also_uses_fill_band_max(self):
        """fill_band_max override applies to DOWN consensus too."""
        surface = _make_down_surface()
        token = _gp.set_active({"fill_band_max": 0.78})
        try:
            decision = evaluate_consensus_4way(surface)
        finally:
            _gp._ACTIVE.reset(token)

        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"
        assert decision.entry_cap == pytest.approx(0.78)
