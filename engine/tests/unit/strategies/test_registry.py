"""Tests for StrategyRegistry -- loads YAML, builds pipelines, evaluates."""

import sys
import os
import tempfile
import time
from pathlib import Path

import pytest
import yaml
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from strategies.registry import StrategyRegistry, StrategyConfig, SizingResult
from strategies.data_surface import DataSurfaceManager, FullDataSurface


def _make_surface(**overrides) -> FullDataSurface:
    """Create a FullDataSurface with sensible defaults."""
    defaults = dict(
        asset="BTC",
        timescale="5m",
        window_ts=1713000000,
        eval_offset=120,
        assembled_at=time.time(),
        current_price=84500.0,
        open_price=84000.0,
        delta_binance=0.005,
        delta_tiingo=0.004,
        delta_chainlink=0.005,
        delta_pct=0.004,
        delta_source="tiingo_rest_candle",
        vpin=0.45,
        regime="NORMAL",
        twap_delta=0.003,
        v2_probability_up=0.38,
        v2_probability_raw=0.36,
        v2_quantiles_p10=None,
        v2_quantiles_p50=None,
        v2_quantiles_p90=None,
        # Audit #121 Path 1 ensemble fields (default None — most tests don't care)
        probability_lgb=None,
        probability_classifier=None,
        ensemble_config=None,
        v3_5m_composite=None,
        v3_15m_composite=None,
        v3_1h_composite=None,
        v3_4h_composite=None,
        v3_24h_composite=None,
        v3_48h_composite=None,
        v3_72h_composite=None,
        v3_1w_composite=None,
        v3_2w_composite=None,
        v3_sub_elm=None,
        v3_sub_cascade=None,
        v3_sub_taker=None,
        v3_sub_oi=None,
        v3_sub_funding=None,
        v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="calm_trend",
        v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BULL",
        v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True,
        v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH",
        v4_conviction_score=0.85,
        poly_direction="DOWN",
        poly_trade_advised=True,
        poly_confidence=0.38,
        poly_confidence_distance=0.12,
        poly_timing="optimal",
        poly_max_entry_price=0.65,
        poly_reason="strong_signal",
        v4_recommended_side="DOWN",
        v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None,
        v4_quantiles=None,
        clob_up_bid=0.46,
        clob_up_ask=0.48,
        clob_down_bid=0.52,
        clob_down_ask=0.54,
        clob_implied_up=0.47,
        gamma_up_price=0.45,
        gamma_down_price=0.55,
        cg_oi_usd=50_000_000.0,
        cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0,
        cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0,
        cg_liq_long=300_000.0,
        cg_liq_short=200_000.0,
        cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0,
        timesfm_vol_forecast_bps=80.0,
        hour_utc=12,
        seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


class FakeWindow:
    def __init__(self, **kwargs):
        self.asset = kwargs.get("asset", "BTC")
        self.window_ts = kwargs.get("window_ts", 1713000000)
        self.open_price = kwargs.get("open_price", 84000.0)
        self.eval_offset = kwargs.get("eval_offset", 120)
        self.up_price = kwargs.get("up_price", 0.45)
        self.down_price = kwargs.get("down_price", 0.55)
        self.timeframe = kwargs.get("timeframe", "5m")


class _FakeExecuteUC:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        return self.result


class TestRegistryLoadYAML:
    def test_load_simple_config(self, tmp_path):
        config = {
            "name": "test_strat",
            "version": "1.0.0",
            "mode": "GHOST",
            "asset": "BTC",
            "timescale": "5m",
            "gates": [
                {"type": "timing", "params": {"min_offset": 90, "max_offset": 150}},
                {"type": "direction", "params": {"direction": "DOWN"}},
            ],
            "sizing": {"type": "fixed_kelly", "fraction": 0.025},
        }
        yaml_path = tmp_path / "test_strat.yaml"
        yaml_path.write_text(yaml.dump(config))

        mgr = DataSurfaceManager(v4_base_url="http://fake")
        registry = StrategyRegistry(str(tmp_path), mgr)
        registry.load_all()

        assert "test_strat" in registry.strategy_names
        assert len(registry.configs) == 1

    def test_load_unknown_gate_raises(self, tmp_path):
        config = {
            "name": "bad_strat",
            "version": "1.0.0",
            "gates": [{"type": "nonexistent_gate", "params": {}}],
            "sizing": {},
        }
        yaml_path = tmp_path / "bad_strat.yaml"
        yaml_path.write_text(yaml.dump(config))

        mgr = DataSurfaceManager(v4_base_url="http://fake")
        registry = StrategyRegistry(str(tmp_path), mgr)
        registry.load_all()

        # Should not crash, but strategy should not be loaded
        assert "bad_strat" not in registry.strategy_names


class TestRegistryEvaluate:
    def _make_registry(self, tmp_path, config_dict):
        yaml_path = tmp_path / f"{config_dict['name']}.yaml"
        yaml_path.write_text(yaml.dump(config_dict))

        mgr = DataSurfaceManager(v4_base_url="http://fake")
        registry = StrategyRegistry(str(tmp_path), mgr)
        registry.load_all()
        return registry

    def test_evaluate_passes_all_gates(self, tmp_path):
        config = {
            "name": "down_test",
            "version": "1.0.0",
            "mode": "GHOST",
            "gates": [
                {"type": "timing", "params": {"min_offset": 90, "max_offset": 150}},
                {"type": "direction", "params": {"direction": "DOWN"}},
                {"type": "confidence", "params": {"min_dist": 0.10}},
            ],
            "sizing": {"type": "fixed_kelly", "fraction": 0.025},
        }
        registry = self._make_registry(tmp_path, config)

        # Evaluate directly using _evaluate_one
        surface = _make_surface(
            eval_offset=120,
            poly_direction="DOWN",
            poly_confidence_distance=0.12,
        )
        decision = registry._evaluate_one(
            "down_test", registry.configs["down_test"], surface
        )
        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"

    def test_evaluate_fails_timing(self, tmp_path):
        config = {
            "name": "timing_fail",
            "version": "1.0.0",
            "mode": "GHOST",
            "gates": [
                {"type": "timing", "params": {"min_offset": 90, "max_offset": 150}},
            ],
            "sizing": {},
        }
        registry = self._make_registry(tmp_path, config)

        surface = _make_surface(eval_offset=60)
        decision = registry._evaluate_one(
            "timing_fail", registry.configs["timing_fail"], surface
        )
        assert decision.action == "SKIP"
        assert "timing" in decision.skip_reason

    def test_disabled_strategy_skipped(self, tmp_path):
        config = {
            "name": "disabled",
            "version": "1.0.0",
            "mode": "DISABLED",
            "gates": [],
            "sizing": {},
        }
        registry = self._make_registry(tmp_path, config)

        import asyncio

        decisions = asyncio.run(registry.evaluate_all(FakeWindow(), None))
        assert len(decisions) == 0

    def test_gate_short_circuits(self, tmp_path):
        """First failing gate should stop pipeline."""
        config = {
            "name": "short_circuit",
            "version": "1.0.0",
            "mode": "GHOST",
            "gates": [
                {"type": "timing", "params": {"min_offset": 200, "max_offset": 300}},
                {"type": "direction", "params": {"direction": "UP"}},
            ],
            "sizing": {},
        }
        registry = self._make_registry(tmp_path, config)

        surface = _make_surface(eval_offset=120)
        decision = registry._evaluate_one(
            "short_circuit", registry.configs["short_circuit"], surface
        )
        assert decision.action == "SKIP"
        # Should fail on timing, not direction
        assert "timing" in decision.skip_reason

    def test_failed_live_execution_does_not_dedup_window(self, tmp_path):
        config = {
            "name": "live_retry",
            "version": "1.0.0",
            "mode": "LIVE",
            "timescale": "15m",
            "gates": [],
            "hooks_file": "live_retry.py",
            "pre_gate_hook": "always_trade",
            "sizing": {"type": "fixed_kelly", "fraction": 0.025},
        }
        (tmp_path / "live_retry.yaml").write_text(yaml.dump(config))
        (tmp_path / "live_retry.py").write_text(
            "from domain.value_objects import StrategyDecision\n"
            "def always_trade(surface):\n"
            "    return StrategyDecision(action='TRADE', direction='DOWN', confidence='HIGH', confidence_score=0.8, entry_cap=0.65, collateral_pct=0.025, strategy_id='live_retry', strategy_version='1.0.0', entry_reason='test', skip_reason=None, metadata={})\n"
        )

        mgr = DataSurfaceManager(v4_base_url="http://fake")
        failed_result = SimpleNamespace(
            success=False,
            order_id=None,
            fill_price=None,
            execution_mode="gtc",
        )
        execute_uc = _FakeExecuteUC(failed_result)
        registry = StrategyRegistry(str(tmp_path), mgr, execute_trade_uc=execute_uc)
        registry.load_all()

        import asyncio

        window = FakeWindow(timeframe="15m", window_ts=1713000000)
        market = SimpleNamespace(up_token_id="up", down_token_id="down")

        asyncio.run(
            registry.evaluate_all(
                window,
                None,
                window_market=market,
                current_btc_price=84000.0,
                open_price=84000.0,
            )
        )
        asyncio.run(
            registry.evaluate_all(
                window,
                None,
                window_market=market,
                current_btc_price=84000.0,
                open_price=84000.0,
            )
        )

        assert execute_uc.calls == 2


# ──────────────────────────────────────────────────────────────────────────────
# v4.4.0: surface-field persistence (registry → db.update_window_surface_fields)
# ──────────────────────────────────────────────────────────────────────────────


def test_v34_surface_fields_extraction():
    """Helper extracts v3/v4 fields from FullDataSurface into the column dict."""
    from strategies.five_min_vpin import _v34_surface_fields

    surface = _make_surface(
        v3_sub_elm=-0.6,
        v3_sub_cascade=-1.0,
        v3_sub_taker=0.2,
        v3_sub_vpin=0.5,
        v3_sub_momentum=-0.1,
        v3_sub_oi=0.05,
        v3_sub_funding=-0.3,
        v4_regime="risk_off",
        v4_regime_confidence=0.9,
        v4_conviction="HIGH",
        v4_conviction_score=0.85,
        v4_consensus_safe_to_trade=False,
        v4_consensus_agreement_score=0.4,
        v4_consensus_max_divergence_bps=18.0,
        v4_macro_bias="NEUTRAL",
        v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
    )

    fields = _v34_surface_fields(surface)

    # Subset of expected mappings
    assert fields["sub_signal_elm"] == -0.6
    assert fields["sub_signal_cascade"] == -1.0
    assert fields["strategy_conviction"] == "HIGH"
    assert fields["strategy_conviction_score"] == 0.85
    assert fields["consensus_safe_to_trade"] is False
    assert fields["consensus_agreement_score"] == 0.4
    assert fields["consensus_divergence_bps"] == 18.0
    assert fields["macro_bias"] == "NEUTRAL"


def test_v34_surface_fields_none_surface():
    """Helper returns {} when surface is None (legacy call path safety)."""
    from strategies.five_min_vpin import _v34_surface_fields

    assert _v34_surface_fields(None) == {}


class TestRegistrySurfacePersistence:
    """Registry should call db.update_window_surface_fields when db is wired."""

    @pytest.mark.asyncio
    async def test_write_window_trace_invokes_db_surface_writer(self, tmp_path):
        from unittest.mock import MagicMock, AsyncMock
        import asyncio

        yaml_path = tmp_path / "surf_test.yaml"
        yaml_path.write_text(
            yaml.dump(
                {
                    "name": "surf_test",
                    "version": "1.0.0",
                    "mode": "GHOST",
                    "gates": [],
                    "sizing": {"type": "fixed_kelly", "fraction": 0.025},
                }
            )
        )

        mgr = DataSurfaceManager(v4_base_url="http://fake")
        mock_trace = MagicMock()
        mock_trace.write_window_evaluation_trace = AsyncMock()
        mock_trace.write_gate_check_traces = AsyncMock()

        mock_db = MagicMock()
        mock_db.update_window_surface_fields = AsyncMock()
        # 2026-04-19: second writer (ensemble surface). Needs AsyncMock so
        # asyncio.create_task accepts the return value.
        mock_db.update_window_ensemble_fields = AsyncMock()

        registry = StrategyRegistry(
            str(tmp_path),
            mgr,
            trace_repo=mock_trace,
            db=mock_db,
        )
        registry.load_all()

        surface = _make_surface(
            v3_sub_elm=-0.7,
            v4_conviction="MEDIUM",
        )

        registry._write_window_trace(surface)
        # Yield so the fire-and-forget asyncio tasks actually run
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        mock_db.update_window_surface_fields.assert_awaited_once()
        call = mock_db.update_window_surface_fields.await_args
        assert call.kwargs["asset"] == "BTC"
        assert call.kwargs["window_ts"] == surface.window_ts
        assert call.kwargs["surface_fields"]["sub_signal_elm"] == -0.7
        assert call.kwargs["surface_fields"]["strategy_conviction"] == "MEDIUM"

    @pytest.mark.asyncio
    async def test_write_window_trace_skips_db_when_all_fields_none(self, tmp_path):
        """Don't fire the DB write when every v3/v4 field is None (no-op surface)."""
        from unittest.mock import MagicMock, AsyncMock
        import asyncio

        yaml_path = tmp_path / "surf_null.yaml"
        yaml_path.write_text(
            yaml.dump(
                {
                    "name": "surf_null",
                    "version": "1.0.0",
                    "mode": "GHOST",
                    "gates": [],
                    "sizing": {"type": "fixed_kelly", "fraction": 0.025},
                }
            )
        )
        mgr = DataSurfaceManager(v4_base_url="http://fake")
        mock_trace = MagicMock()
        mock_trace.write_window_evaluation_trace = AsyncMock()
        mock_db = MagicMock()
        mock_db.update_window_surface_fields = AsyncMock()
        # 2026-04-19: second writer (ensemble surface). Needs AsyncMock so
        # asyncio.create_task accepts the return value when it fires.
        mock_db.update_window_ensemble_fields = AsyncMock()

        registry = StrategyRegistry(
            str(tmp_path), mgr, trace_repo=mock_trace, db=mock_db
        )
        registry.load_all()

        surface = _make_surface(
            v3_sub_elm=None,
            v3_sub_cascade=None,
            v3_sub_taker=None,
            v3_sub_vpin=None,
            v3_sub_momentum=None,
            v3_sub_oi=None,
            v3_sub_funding=None,
            v4_regime_confidence=None,
            v4_regime_persistence=None,
            v4_conviction=None,
            v4_conviction_score=None,
            v4_consensus_safe_to_trade=None,
            v4_consensus_agreement_score=None,
            v4_consensus_max_divergence_bps=None,
            v4_macro_bias=None,
            v4_macro_direction_gate=None,
            v4_macro_size_modifier=None,
            # Ensemble fields also all None → both writers skipped.
            v2_probability_up=None,
            probability_lgb=None,
            probability_classifier=None,
            ensemble_config=None,
        )
        registry._write_window_trace(surface)
        await asyncio.sleep(0)

        mock_db.update_window_surface_fields.assert_not_awaited()
        mock_db.update_window_ensemble_fields.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_write_window_trace_no_db_is_noop(self, tmp_path):
        """When db is None the surface-fields path silently skips."""
        from unittest.mock import MagicMock, AsyncMock
        import asyncio

        yaml_path = tmp_path / "surf_nodb.yaml"
        yaml_path.write_text(
            yaml.dump(
                {
                    "name": "surf_nodb",
                    "version": "1.0.0",
                    "mode": "GHOST",
                    "gates": [],
                    "sizing": {"type": "fixed_kelly", "fraction": 0.025},
                }
            )
        )
        mgr = DataSurfaceManager(v4_base_url="http://fake")
        mock_trace = MagicMock()
        mock_trace.write_window_evaluation_trace = AsyncMock()

        registry = StrategyRegistry(
            str(tmp_path), mgr, trace_repo=mock_trace, db=None
        )
        registry.load_all()

        surface = _make_surface(v3_sub_elm=-0.5)
        # Should not raise — db=None path just skips
        registry._write_window_trace(surface)
        await asyncio.sleep(0)


# ──────────────────────────────────────────────────────────────────────────────
# 2026-04-19: v5_ensemble probability-surface persistence
# (registry → db.update_window_ensemble_fields). Mirrors the v3/v4
# `_v34_surface_fields` tests above. Unblocks historical counterfactual
# WR across browser reloads / operators — see migration header comment.
# ──────────────────────────────────────────────────────────────────────────────


def test_ensemble_surface_fields_full_blend():
    """Full-ensemble shape: all components + mode=blend + config-reported disagreement."""
    from strategies.five_min_vpin import _ensemble_surface_fields

    surface = _make_surface(
        v2_probability_up=0.62,
        probability_lgb=0.58,
        probability_classifier=0.70,
        ensemble_config={
            "mode": "blend",
            "weights": {"lgb": 0.5, "classifier": 0.5},
            "disagreement_magnitude": 0.12,
            "disagreement_detected": True,
            "model_version": "path1-classifier-v0.3.1",
        },
    )

    fields = _ensemble_surface_fields(surface)

    assert fields["ensemble_p_up"] == 0.62
    assert fields["ensemble_p_lgb"] == 0.58
    assert fields["ensemble_p_classifier"] == 0.70
    assert fields["ensemble_mode"] == "blend"
    assert fields["ensemble_disagreement"] == 0.12
    assert fields["ensemble_model_version"] == "path1-classifier-v0.3.1"


def test_ensemble_surface_fields_fallback_lgb_only():
    """Classifier head unavailable: p_classifier NULL, disagreement NULL, mode reported."""
    from strategies.five_min_vpin import _ensemble_surface_fields

    surface = _make_surface(
        v2_probability_up=0.55,
        probability_lgb=0.55,
        probability_classifier=None,
        ensemble_config={
            "mode": "fallback_lgb_only",
            "model_version": "path1-classifier-v0.3.1",
        },
    )

    fields = _ensemble_surface_fields(surface)

    assert fields["ensemble_p_up"] == 0.55
    assert fields["ensemble_p_lgb"] == 0.55
    assert fields["ensemble_p_classifier"] is None
    assert fields["ensemble_mode"] == "fallback_lgb_only"
    # No disagreement when one component is missing and config omits it.
    assert fields["ensemble_disagreement"] is None
    # model_version survives the fallback.
    assert fields["ensemble_model_version"] == "path1-classifier-v0.3.1"


def test_ensemble_surface_fields_config_absent():
    """ensemble_config None: components + mode + disagreement NULL, p_up preserved."""
    from strategies.five_min_vpin import _ensemble_surface_fields

    surface = _make_surface(
        v2_probability_up=0.48,
        probability_lgb=None,
        probability_classifier=None,
        ensemble_config=None,
    )

    fields = _ensemble_surface_fields(surface)

    assert fields["ensemble_p_up"] == 0.48
    assert fields["ensemble_p_lgb"] is None
    assert fields["ensemble_p_classifier"] is None
    assert fields["ensemble_mode"] is None
    assert fields["ensemble_disagreement"] is None
    assert fields["ensemble_model_version"] is None


def test_ensemble_surface_fields_disagreement_derived():
    """Config omits disagreement_magnitude: derive as |p_lgb - p_classifier|."""
    from strategies.five_min_vpin import _ensemble_surface_fields

    surface = _make_surface(
        v2_probability_up=0.60,
        probability_lgb=0.55,
        probability_classifier=0.71,
        ensemble_config={"mode": "blend"},  # no disagreement_magnitude
    )

    fields = _ensemble_surface_fields(surface)

    assert fields["ensemble_disagreement"] == pytest.approx(0.16, rel=1e-6)
    assert fields["ensemble_mode"] == "blend"


def test_ensemble_surface_fields_none_surface():
    """Helper returns {} when surface is None (matches _v34_surface_fields pattern)."""
    from strategies.five_min_vpin import _ensemble_surface_fields

    assert _ensemble_surface_fields(None) == {}


class TestRegistryEnsemblePersistence:
    """Registry should call db.update_window_ensemble_fields for each of the
    three snapshot shapes spec'd in the write-path design:
      1. Full ensemble (all fields populated, mode='blend')
      2. Fallback (ensemble_config.mode='fallback_lgb_only', classifier NULL)
      3. Missing snapshot entirely (all 6 NULL → DB call skipped)
    """

    def _load_registry(self, tmp_path, *, db, trace):
        yaml_path = tmp_path / "ens_test.yaml"
        yaml_path.write_text(
            yaml.dump(
                {
                    "name": "ens_test",
                    "version": "1.0.0",
                    "mode": "GHOST",
                    "gates": [],
                    "sizing": {"type": "fixed_kelly", "fraction": 0.025},
                }
            )
        )
        mgr = DataSurfaceManager(v4_base_url="http://fake")
        registry = StrategyRegistry(
            str(tmp_path), mgr, trace_repo=trace, db=db
        )
        registry.load_all()
        return registry

    @pytest.mark.asyncio
    async def test_full_ensemble_invokes_writer(self, tmp_path):
        """Snapshot shape #1 — all fields populated."""
        from unittest.mock import MagicMock, AsyncMock
        import asyncio

        mock_trace = MagicMock()
        mock_trace.write_window_evaluation_trace = AsyncMock()
        mock_trace.write_gate_check_traces = AsyncMock()

        mock_db = MagicMock()
        mock_db.update_window_surface_fields = AsyncMock()
        mock_db.update_window_ensemble_fields = AsyncMock()

        registry = self._load_registry(tmp_path, db=mock_db, trace=mock_trace)

        surface = _make_surface(
            v2_probability_up=0.62,
            probability_lgb=0.58,
            probability_classifier=0.70,
            ensemble_config={
                "mode": "blend",
                "disagreement_magnitude": 0.12,
                "model_version": "path1-classifier-v0.3.1",
            },
        )

        registry._write_window_trace(surface)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        mock_db.update_window_ensemble_fields.assert_awaited_once()
        call = mock_db.update_window_ensemble_fields.await_args
        f = call.kwargs["ensemble_fields"]
        assert f["ensemble_p_up"] == 0.62
        assert f["ensemble_p_lgb"] == 0.58
        assert f["ensemble_p_classifier"] == 0.70
        assert f["ensemble_mode"] == "blend"
        assert f["ensemble_disagreement"] == 0.12
        assert f["ensemble_model_version"] == "path1-classifier-v0.3.1"

    @pytest.mark.asyncio
    async def test_fallback_lgb_only_invokes_writer(self, tmp_path):
        """Snapshot shape #2 — classifier NULL, mode=fallback_lgb_only."""
        from unittest.mock import MagicMock, AsyncMock
        import asyncio

        mock_trace = MagicMock()
        mock_trace.write_window_evaluation_trace = AsyncMock()
        mock_trace.write_gate_check_traces = AsyncMock()

        mock_db = MagicMock()
        mock_db.update_window_surface_fields = AsyncMock()
        mock_db.update_window_ensemble_fields = AsyncMock()

        registry = self._load_registry(tmp_path, db=mock_db, trace=mock_trace)

        surface = _make_surface(
            v2_probability_up=0.55,
            probability_lgb=0.55,
            probability_classifier=None,
            ensemble_config={
                "mode": "fallback_lgb_only",
                "model_version": "path1-classifier-v0.3.1",
            },
        )

        registry._write_window_trace(surface)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        mock_db.update_window_ensemble_fields.assert_awaited_once()
        f = mock_db.update_window_ensemble_fields.await_args.kwargs[
            "ensemble_fields"
        ]
        assert f["ensemble_p_up"] == 0.55
        assert f["ensemble_p_lgb"] == 0.55
        assert f["ensemble_p_classifier"] is None
        assert f["ensemble_mode"] == "fallback_lgb_only"
        assert f["ensemble_disagreement"] is None
        assert f["ensemble_model_version"] == "path1-classifier-v0.3.1"

    @pytest.mark.asyncio
    async def test_missing_snapshot_skips_writer(self, tmp_path):
        """Snapshot shape #3 — all six fields NULL → writer NOT called.

        Rationale: no useful information in an all-NULL UPDATE; skipping
        avoids pointless DB hits. The legacy writer still creates the row
        with the usual columns when a trade evaluates.
        """
        from unittest.mock import MagicMock, AsyncMock
        import asyncio

        mock_trace = MagicMock()
        mock_trace.write_window_evaluation_trace = AsyncMock()
        mock_trace.write_gate_check_traces = AsyncMock()

        mock_db = MagicMock()
        mock_db.update_window_surface_fields = AsyncMock()
        mock_db.update_window_ensemble_fields = AsyncMock()

        registry = self._load_registry(tmp_path, db=mock_db, trace=mock_trace)

        # Override every v2/ensemble field AND every v3/v4 field so both
        # writers have nothing to flush — guarantees the second task
        # (ensemble) is never scheduled and we can assert it wasn't called.
        surface = _make_surface(
            v2_probability_up=None,
            probability_lgb=None,
            probability_classifier=None,
            ensemble_config=None,
        )
        registry._write_window_trace(surface)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        mock_db.update_window_ensemble_fields.assert_not_awaited()


class TestCheckSourceAgreement:
    """Pinned behaviour for ``StrategyRegistry._check_source_agreement``.

    Matches the trade-gate definition (Chainlink + Tiingo only). Binance
    is diagnostic-only and MUST NOT influence the consensus verdict.
    """

    def test_both_up_returns_yes_up(self):
        surface = _make_surface(
            delta_chainlink=0.0004, delta_tiingo=0.0005, delta_binance=-0.0002
        )
        # Binance disagreeing must NOT tip the verdict to "mixed" -- the
        # gate ignores Binance, so the consensus string must too.
        assert StrategyRegistry._check_source_agreement(surface) == "YES (UP)"

    def test_both_down_returns_yes_down(self):
        surface = _make_surface(
            delta_chainlink=-0.0003, delta_tiingo=-0.0004, delta_binance=0.0008
        )
        assert StrategyRegistry._check_source_agreement(surface) == "YES (DOWN)"

    def test_disagree_reports_per_source_directions(self):
        surface = _make_surface(delta_chainlink=-0.0006, delta_tiingo=0.0005)
        assert (
            StrategyRegistry._check_source_agreement(surface)
            == "NO (CL=DOWN, Ti=UP)"
        )

    def test_missing_chainlink_returns_unknown_with_reason(self):
        surface = _make_surface(delta_chainlink=None, delta_tiingo=0.0005)
        assert (
            StrategyRegistry._check_source_agreement(surface)
            == "unknown (chainlink missing)"
        )

    def test_missing_both_reports_both(self):
        surface = _make_surface(delta_chainlink=None, delta_tiingo=None)
        assert (
            StrategyRegistry._check_source_agreement(surface)
            == "unknown (chainlink+tiingo missing)"
        )


class TestFormatBinanceCrossCheck:
    """Binance cross-check is rendered as a separate diagnostic line."""

    def test_positive_delta_renders_with_up_tag(self):
        surface = _make_surface(delta_binance=0.00057)
        assert (
            StrategyRegistry._format_binance_cross_check(surface)
            == "+0.06% (UP)"
        )

    def test_negative_delta_renders_with_down_tag(self):
        surface = _make_surface(delta_binance=-0.00019)
        assert (
            StrategyRegistry._format_binance_cross_check(surface)
            == "-0.02% (DOWN)"
        )

    def test_none_returns_none(self):
        surface = _make_surface(delta_binance=None)
        assert StrategyRegistry._format_binance_cross_check(surface) is None
