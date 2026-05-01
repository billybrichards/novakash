"""Unit tests for v8_v12_strong_agree hook — strong v9/v12 LGB agreement gate.

Covers all SKIP paths + delegation path. The downstream v8_champion_lgb_only
stack is mocked out via monkeypatch so we can assert on the gate's own
behaviour (direction agreement + strong-conviction floors) without dragging
in the full v9_ensemble pipeline.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

# Set required env vars before any engine imports trigger Settings()
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from strategies.data_surface import FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    """Build a minimal FullDataSurface for testing v8_v12_strong_agree."""
    defaults = dict(
        asset="BTC", timescale="5m", window_ts=1713010800,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=85000.0,
        delta_binance=-0.005, delta_tiingo=-0.004, delta_chainlink=-0.005,
        delta_pct=-0.005, delta_source="chainlink",
        vpin=0.55, regime="NORMAL", twap_delta=-0.003,
        v2_probability_up=0.30, v2_probability_raw=0.30,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        probability_lgb=None, probability_classifier=None,
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
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


def _stub_v8_lgb_decision(action: str = "TRADE", direction: str = "UP"):
    """Return a callable that mimics evaluate_v8_champion_lgb_only.

    Captures probability_lgb at call time so tests can assert the swap
    happened.
    """
    from domain.value_objects import StrategyDecision

    captured: dict = {}

    def _stub(surface):
        captured["probability_lgb"] = getattr(surface, "probability_lgb", None)
        return StrategyDecision(
            action=action,
            direction=direction if action == "TRADE" else None,
            confidence="HIGH" if action == "TRADE" else None,
            confidence_score=0.5 if action == "TRADE" else 0.0,
            entry_cap=0.80 if action == "TRADE" else None,
            collateral_pct=0.025 if action == "TRADE" else None,
            strategy_id="v8_champion_lgb_only",
            strategy_version="8.0.0-lgb-0.2",
            entry_reason=(
                f"v8_champion_lgb_only_{direction}_T120_f0.55"
                if action == "TRADE"
                else ""
            ),
            skip_reason=None if action == "TRADE" else "stub_skip",
            metadata={"stub": True, "gate_results": []},
        )

    return _stub, captured


class TestV8V12StrongAgreeGate:
    """Gate-only tests — downstream v8 stack stubbed."""

    def test_skip_when_v9_missing(self, monkeypatch):
        from strategies.configs import v8_v12_strong_agree as mod

        stub, _ = _stub_v8_lgb_decision()
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=None, probability_lgb_v12=0.70)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "missing v9 or v12 lgb prob"
        assert decision.strategy_id == "v8_v12_strong_agree"
        assert decision.strategy_version == "1.0.0"
        assert decision.metadata["probability_lgb"] is None
        assert decision.metadata["probability_lgb_v12"] == 0.70

    def test_skip_when_v12_missing(self, monkeypatch):
        from strategies.configs import v8_v12_strong_agree as mod

        stub, _ = _stub_v8_lgb_decision()
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.70, probability_lgb_v12=None)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "missing v9 or v12 lgb prob"

    def test_skip_when_directions_disagree(self, monkeypatch):
        """v9 says UP (0.75), v12 says DOWN (0.30) -> SKIP."""
        from strategies.configs import v8_v12_strong_agree as mod

        stub, _ = _stub_v8_lgb_decision()
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.75, probability_lgb_v12=0.30)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "v9_v12 direction disagreement"
        assert decision.metadata["dir_v9"] == "UP"
        assert decision.metadata["dir_v12"] == "DOWN"
        assert decision.metadata["direction_agree"] is False

    def test_skip_up_below_threshold(self, monkeypatch):
        """Both UP at 0.60 (< 0.65 threshold) -> SKIP."""
        from strategies.configs import v8_v12_strong_agree as mod

        stub, captured = _stub_v8_lgb_decision()
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.60, probability_lgb_v12=0.60)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "below_strong_agree_up_threshold"
        assert decision.metadata["dir_v9"] == "UP"
        assert decision.metadata["dir_v12"] == "UP"
        assert decision.metadata["direction_agree"] is True
        assert decision.metadata["up_min"] == 0.65
        # Stub must NOT have been called when gate skipped.
        assert "probability_lgb" not in captured

    def test_skip_up_when_only_one_below_threshold(self, monkeypatch):
        """v9=0.70 (passes), v12=0.62 (below 0.65) -> SKIP (BOTH must pass)."""
        from strategies.configs import v8_v12_strong_agree as mod

        stub, _ = _stub_v8_lgb_decision()
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.70, probability_lgb_v12=0.62)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "below_strong_agree_up_threshold"

    def test_skip_down_above_threshold(self, monkeypatch):
        """Both DOWN at 0.40 (> 0.35 max) -> SKIP."""
        from strategies.configs import v8_v12_strong_agree as mod

        stub, captured = _stub_v8_lgb_decision()
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.40, probability_lgb_v12=0.40)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "below_strong_agree_down_threshold"
        assert decision.metadata["dir_v9"] == "DOWN"
        assert decision.metadata["dir_v12"] == "DOWN"
        assert decision.metadata["direction_agree"] is True
        assert decision.metadata["down_max"] == 0.35
        # Edge: 0.30 v9 passes (<= 0.35) but 0.40 v12 fails -> still SKIP.
        assert "probability_lgb" not in captured

    def test_skip_down_when_only_one_above_threshold(self, monkeypatch):
        """v9=0.30 (passes), v12=0.40 (> 0.35) -> SKIP (BOTH must pass)."""
        from strategies.configs import v8_v12_strong_agree as mod

        stub, _ = _stub_v8_lgb_decision()
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.30, probability_lgb_v12=0.40)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "below_strong_agree_down_threshold"

    def test_delegate_when_both_up_above_threshold(self, monkeypatch):
        """Both UP at 0.70 (>= 0.65) -> delegate with averaged prob."""
        from strategies.configs import v8_v12_strong_agree as mod

        stub, captured = _stub_v8_lgb_decision(action="TRADE", direction="UP")
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.70, probability_lgb_v12=0.70)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        # Stub WAS called; surface's probability_lgb at call time was the avg
        assert captured["probability_lgb"] == pytest.approx(0.70)
        assert decision.action == "TRADE"
        assert decision.direction == "UP"
        assert decision.strategy_id == "v8_v12_strong_agree"
        assert decision.strategy_version == "1.0.0"
        assert decision.metadata["probability_lgb"] == 0.70
        assert decision.metadata["probability_lgb_v12"] == 0.70
        assert decision.metadata["p_combo"] == pytest.approx(0.70)
        assert decision.metadata["dir_v9"] == "UP"
        assert decision.metadata["direction_agree"] is True
        assert decision.metadata["v8_v12_strong_agree"] is True

    def test_delegate_when_both_down_below_threshold(self, monkeypatch):
        """Both DOWN at 0.25 (<= 0.35) -> delegate with averaged prob."""
        from strategies.configs import v8_v12_strong_agree as mod

        stub, captured = _stub_v8_lgb_decision(action="TRADE", direction="DOWN")
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.25, probability_lgb_v12=0.25)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        assert captured["probability_lgb"] == pytest.approx(0.25)
        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"
        assert decision.strategy_id == "v8_v12_strong_agree"
        assert decision.metadata["p_combo"] == pytest.approx(0.25)
        assert decision.metadata["dir_v9"] == "DOWN"
        assert decision.metadata["direction_agree"] is True
        assert decision.metadata["v8_v12_strong_agree"] is True

    def test_delegate_uses_averaged_prob(self, monkeypatch):
        """v9=0.65, v12=0.75 (both >= 0.65) -> delegate with avg=0.70."""
        from strategies.configs import v8_v12_strong_agree as mod

        stub, captured = _stub_v8_lgb_decision(action="TRADE", direction="UP")
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.65, probability_lgb_v12=0.75)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        assert captured["probability_lgb"] == pytest.approx(0.70)
        assert decision.metadata["p_combo"] == pytest.approx(0.70)

    def test_surface_restored_after_delegation(self, monkeypatch):
        """Original probability_lgb is restored after the delegate runs."""
        from strategies.configs import v8_v12_strong_agree as mod

        stub, _ = _stub_v8_lgb_decision(action="TRADE", direction="UP")
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        original_v9 = 0.70
        surface = _make_surface(
            probability_lgb=original_v9,
            probability_lgb_v12=0.80,
        )
        mod.evaluate_v8_v12_strong_agree(surface)

        # Surface's probability_lgb is restored
        assert surface.probability_lgb == original_v9

    def test_thresholds_overridable_via_gate_params(self, monkeypatch):
        """Tighter UP threshold (0.75) blocks 0.70/0.70 that would pass default."""
        from strategies import gate_params as _gp
        from strategies.configs import v8_v12_strong_agree as mod

        stub, captured = _stub_v8_lgb_decision()
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.70, probability_lgb_v12=0.70)
        token = _gp.set_active({"combo_strong_agree_up_min": 0.75})
        try:
            decision = mod.evaluate_v8_v12_strong_agree(surface)
        finally:
            _gp.reset_active(token)

        assert decision.action == "SKIP"
        assert decision.skip_reason == "below_strong_agree_up_threshold"
        assert decision.metadata["up_min"] == 0.75
        assert "probability_lgb" not in captured


class TestV8V12StrongAgreeIdentity:
    """Strategy-identity invariants — strategy_id/version preserved on every path."""

    def test_strategy_id_preserved_on_skip(self, monkeypatch):
        from strategies.configs import v8_v12_strong_agree as mod

        stub, _ = _stub_v8_lgb_decision()
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        for surface in [
            _make_surface(probability_lgb=None, probability_lgb_v12=0.70),
            _make_surface(probability_lgb=0.75, probability_lgb_v12=0.30),
            _make_surface(probability_lgb=0.60, probability_lgb_v12=0.60),
            _make_surface(probability_lgb=0.40, probability_lgb_v12=0.40),
        ]:
            decision = mod.evaluate_v8_v12_strong_agree(surface)
            assert decision.strategy_id == "v8_v12_strong_agree"
            assert decision.strategy_version == "1.0.0"

    def test_strategy_id_preserved_on_delegation(self, monkeypatch):
        from strategies.configs import v8_v12_strong_agree as mod

        stub, _ = _stub_v8_lgb_decision(action="TRADE", direction="UP")
        monkeypatch.setattr(mod, "_evaluate_v8_lgb", stub)

        surface = _make_surface(probability_lgb=0.70, probability_lgb_v12=0.70)
        decision = mod.evaluate_v8_v12_strong_agree(surface)

        # Even though stub returns strategy_id='v8_champion_lgb_only',
        # the wrapper rewrites to v8_v12_strong_agree.
        assert decision.strategy_id == "v8_v12_strong_agree"
        assert decision.strategy_version == "1.0.0"
