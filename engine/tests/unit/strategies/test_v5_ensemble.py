"""Tests for v5_ensemble (audit #121 Path 1) — covers the 6 cases from
the handoff spec test plan.

Hooks live in `engine/strategies/configs/v5_ensemble.py` and are loaded
via importlib by `StrategyRegistry`. We exercise them through the
registry the same way `test_strategy_configs.py` does for the existing
strategies — that way we get the real load path under test, not just
the function in isolation.

The hook reads its env vars at module import time, so each test that
needs a non-default config calls `_reload_hook` after `monkeypatch.setenv`
to pick up the new value.
"""

from __future__ import annotations

import importlib
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import StrategyRegistry

CONFIGS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "strategies", "configs"
)


def _make_surface(**overrides) -> FullDataSurface:
    """Default surface that lands on the polymarket_v2 path with all gates green.

    Mirrors `test_strategy_configs._make_surface` but locked to a
    direction=DOWN trade with HIGH conviction so v5_ensemble would TRADE
    when no ensemble-specific gate fires.
    """
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713000000,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.004, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.38, v2_probability_raw=0.36,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        # Path 1 ensemble fields — overridden per test
        probability_lgb=None, probability_classifier=None, ensemble_config=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="calm_trend", v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BEAR", v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.30,
        poly_confidence_distance=0.20, poly_timing="optimal",
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


def _fresh_registry():
    """Build a registry that re-loads v5_ensemble.py with current env vars.

    `StrategyRegistry._load_hooks` uses `importlib.util.spec_from_file_location`
    with module name `strategy_hooks.v5_ensemble`. We force a fresh import by
    popping the cached module before reload, so per-test env var changes are
    actually picked up by the hook's module-level constants.
    """
    sys.modules.pop("strategy_hooks.v5_ensemble", None)
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    reg = StrategyRegistry(CONFIGS_DIR, mgr)
    reg.load_all()
    return reg


def _evaluate(reg, surface):
    return reg._evaluate_one(
        "v5_ensemble", reg.configs["v5_ensemble"], surface
    )


# ─────────────────────────────────────────────────────────────────────────────


class TestSignalSourceSelection:
    """Spec §Test plan #1 — V5_ENSEMBLE_SIGNAL_SOURCE picks the right field."""

    def test_default_ensemble_source_uses_poly_confidence(self, monkeypatch):
        """Default 'ensemble' reads surface.poly_confidence (the blended value)."""
        monkeypatch.delenv("V5_ENSEMBLE_SIGNAL_SOURCE", raising=False)
        reg = _fresh_registry()
        # poly_confidence=0.30 (dist=0.20) — strong DOWN
        # p_lgb / p_classifier intentionally close to 0.5 so if the hook
        # mistakenly read either of them, distance would collapse below the
        # 0.12 confidence threshold and the trade would skip.
        surface = _make_surface(
            poly_confidence=0.30,
            probability_lgb=0.51, probability_classifier=0.52,
            ensemble_config={"mode": "blend"},
        )
        decision = _evaluate(reg, surface)
        assert decision.action == "TRADE"
        assert decision.metadata["signal_source"] == "ensemble"
        assert decision.metadata["probability_used"] == pytest.approx(0.30)

    def test_lgb_only_source_reads_probability_lgb(self, monkeypatch):
        """`lgb_only` ignores poly_confidence and reads probability_lgb."""
        monkeypatch.setenv("V5_ENSEMBLE_SIGNAL_SOURCE", "lgb_only")
        reg = _fresh_registry()
        # poly_confidence=0.50 (dist=0) would normally skip on confidence
        # gate. probability_lgb=0.30 (dist=0.20) lets the trade through —
        # proves the source override works.
        surface = _make_surface(
            poly_confidence=0.50,
            probability_lgb=0.30, probability_classifier=0.50,
            ensemble_config={"mode": "blend"},
        )
        decision = _evaluate(reg, surface)
        assert decision.action == "TRADE"
        assert decision.metadata["signal_source"] == "lgb_only"
        assert decision.metadata["probability_used"] == pytest.approx(0.30)

    def test_path1_only_skips_when_classifier_unavailable(self, monkeypatch):
        """`path1_only` with probability_classifier=None → skip."""
        monkeypatch.setenv("V5_ENSEMBLE_SIGNAL_SOURCE", "path1_only")
        reg = _fresh_registry()
        surface = _make_surface(
            probability_lgb=0.30, probability_classifier=None,
            ensemble_config={"mode": "fallback_lgb_only"},
        )
        decision = _evaluate(reg, surface)
        assert decision.action == "SKIP"
        assert "path1_only" in decision.skip_reason


class TestFallbackSanityGate:
    """Spec §Test plan #2 — ensemble_config.mode=='fallback_lgb_only' skips."""

    def test_skip_on_fallback_when_flag_default_true(self, monkeypatch):
        monkeypatch.delenv("V5_ENSEMBLE_SKIP_ON_FALLBACK", raising=False)
        monkeypatch.delenv("V5_ENSEMBLE_SIGNAL_SOURCE", raising=False)
        reg = _fresh_registry()
        surface = _make_surface(
            poly_confidence=0.30,  # would otherwise pass confidence gate
            probability_lgb=0.30, probability_classifier=None,
            ensemble_config={"mode": "fallback_lgb_only"},
        )
        decision = _evaluate(reg, surface)
        assert decision.action == "SKIP"
        assert decision.skip_reason == "ensemble_fallback_lgb_only"

    def test_pass_on_fallback_when_flag_disabled(self, monkeypatch):
        monkeypatch.setenv("V5_ENSEMBLE_SKIP_ON_FALLBACK", "false")
        monkeypatch.delenv("V5_ENSEMBLE_SIGNAL_SOURCE", raising=False)
        reg = _fresh_registry()
        surface = _make_surface(
            poly_confidence=0.30,
            probability_lgb=0.30, probability_classifier=None,
            ensemble_config={"mode": "fallback_lgb_only"},
        )
        decision = _evaluate(reg, surface)
        assert decision.action == "TRADE"


class TestDisagreementGate:
    """Spec §Test plan #3 + #4 — disagreement gate off-by-default; on at 0.15."""

    def test_disagreement_off_by_default(self, monkeypatch):
        """Threshold=0 → gate doesn't fire even with large disagreement."""
        monkeypatch.delenv("V5_ENSEMBLE_DISAGREEMENT_THRESHOLD", raising=False)
        monkeypatch.delenv("V5_ENSEMBLE_SIGNAL_SOURCE", raising=False)
        reg = _fresh_registry()
        # |0.30 - 0.80| = 0.50 — would skip if the gate fired
        surface = _make_surface(
            poly_confidence=0.30,
            probability_lgb=0.30, probability_classifier=0.80,
            ensemble_config={"mode": "blend"},
        )
        decision = _evaluate(reg, surface)
        assert decision.action == "TRADE"

    def test_disagreement_skip_when_above_threshold(self, monkeypatch):
        monkeypatch.setenv("V5_ENSEMBLE_DISAGREEMENT_THRESHOLD", "0.15")
        monkeypatch.delenv("V5_ENSEMBLE_SIGNAL_SOURCE", raising=False)
        reg = _fresh_registry()
        surface = _make_surface(
            poly_confidence=0.30,
            probability_lgb=0.30, probability_classifier=0.55,  # |Δ|=0.25 > 0.15
            ensemble_config={"mode": "blend"},
        )
        decision = _evaluate(reg, surface)
        assert decision.action == "SKIP"
        assert "ensemble_disagreement" in decision.skip_reason

    def test_disagreement_passes_when_below_threshold(self, monkeypatch):
        monkeypatch.setenv("V5_ENSEMBLE_DISAGREEMENT_THRESHOLD", "0.15")
        monkeypatch.delenv("V5_ENSEMBLE_SIGNAL_SOURCE", raising=False)
        reg = _fresh_registry()
        surface = _make_surface(
            poly_confidence=0.30,
            probability_lgb=0.30, probability_classifier=0.40,  # |Δ|=0.10 <= 0.15
            ensemble_config={"mode": "blend"},
        )
        decision = _evaluate(reg, surface)
        assert decision.action == "TRADE"


class TestBackwardCompat:
    """Spec §Test plan #5 — surface without ensemble fields still works.

    With the dataclass change all surfaces have the 3 fields, but they may
    be None when the timesfm side has ensemble disabled. That is the
    real backward-compat scenario this test covers.
    """

    def test_trade_when_all_ensemble_fields_none(self, monkeypatch):
        monkeypatch.delenv("V5_ENSEMBLE_SIGNAL_SOURCE", raising=False)
        reg = _fresh_registry()
        surface = _make_surface(
            poly_confidence=0.30,
            probability_lgb=None, probability_classifier=None,
            ensemble_config=None,
        )
        decision = _evaluate(reg, surface)
        assert decision.action == "TRADE"
        assert decision.metadata["probability_lgb"] is None
        assert decision.metadata["probability_classifier"] is None
        assert decision.metadata["ensemble_config"] is None


class TestInheritedGates:
    """Spec §Test plan #6 — inherited v4_fusion gates still fire identically.

    Pick the two highest-impact gates: confidence (the one immediately
    before our additions) and chainlink_agreement (an example of a gate
    that runs after).
    """

    def test_inherited_confidence_gate_skips_low_distance(self, monkeypatch):
        monkeypatch.delenv("V5_ENSEMBLE_SIGNAL_SOURCE", raising=False)
        reg = _fresh_registry()
        # poly_confidence=0.55 → dist=0.05 < 0.12 threshold
        surface = _make_surface(poly_confidence=0.55)
        decision = _evaluate(reg, surface)
        assert decision.action == "SKIP"
        assert "0.12 threshold" in decision.skip_reason

    def test_inherited_chainlink_disagrees_skips(self, monkeypatch):
        monkeypatch.delenv("V5_ENSEMBLE_SIGNAL_SOURCE", raising=False)
        reg = _fresh_registry()
        # Trade direction=DOWN, chainlink positive → disagreement → skip
        surface = _make_surface(
            poly_confidence=0.30,
            delta_chainlink=+0.005, delta_tiingo=-0.004,
        )
        decision = _evaluate(reg, surface)
        assert decision.action == "SKIP"
        assert "chainlink_disagrees" in decision.skip_reason


class TestRegistryLoad:
    """Sanity: the YAML actually loads and registers under the right name."""

    def test_v5_ensemble_loads_with_live_mode(self):
        reg = _fresh_registry()
        assert "v5_ensemble" in reg.strategy_names
        # 2026-04-18: GHOST → LIVE flip. Path 1 classifier deployed on
        # Montreal-timesfm; v5 trades alongside v4_fusion.
        assert reg.configs["v5_ensemble"].mode == "LIVE"
