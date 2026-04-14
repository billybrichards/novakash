"""
Integration tests for StrategyRegistry.

Tests YAML config loading, strategy evaluation, and hot-reload functionality.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from margin_engine.application.services.strategy import TradeDecision
from margin_engine.strategies.registry import StrategyRegistry
from margin_engine.domain.value_objects import (
    Consensus,
    MacroBias,
    Quantiles,
    TimescalePayload,
    V4Snapshot,
)


class TestStrategyRegistryYAMLLoading:
    """Integration tests for YAML config loading."""

    def test_load_single_strategy(self):
        """Test loading a single strategy from YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "test_strategy.yaml"
            config_path.write_text("""
name: regime_trend
mode: ACTIVE
timescale: 15m
version: 1.0
params:
  min_probability: 0.55
  size_multiplier: 1.2
  stop_bps: 150
  tp_bps: 200
  hold_minutes: 60
  min_expected_move_bps: 30.0
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            assert "regime_trend" in registry.get_strategy_names()
            assert registry.get_mode("regime_trend") == "ACTIVE"
            assert (
                registry.get_config("regime_trend")["params"]["min_probability"] == 0.55
            )

    def test_load_multiple_strategies(self):
        """Test loading multiple strategies from YAML files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Strategy 1
            config1 = Path(tmpdir) / "trend.yaml"
            config1.write_text("""
name: regime_trend
mode: ACTIVE
timescale: 15m
params:
  min_probability: 0.55
""")

            # Strategy 2
            config2 = Path(tmpdir) / "mean_reversion.yaml"
            config2.write_text("""
name: regime_mean_reversion
mode: DRY_RUN
timescale: 1h
params:
  entry_threshold: 0.70
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            assert len(registry.get_strategy_names()) == 2
            assert registry.get_mode("regime_trend") == "ACTIVE"
            assert registry.get_mode("regime_mean_reversion") == "DRY_RUN"

    def test_load_disabled_strategy(self):
        """Test that disabled strategies are loaded but not evaluated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "disabled.yaml"
            config_path.write_text("""
name: regime_trend
mode: DISABLED
timescale: 15m
params:
  min_probability: 0.55
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            assert "regime_trend" in registry.get_strategy_names()
            assert registry.get_mode("regime_trend") == "DISABLED"
            assert "regime_trend" not in registry.get_active_strategies()

    def test_load_cascade_fade_strategy(self):
        """Test loading cascade fade strategy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "cascade_fade.yaml"
            config_path.write_text("""
name: cascade_fade
mode: ACTIVE
timescale: 5m
params:
  min_strength: 0.5
  entry_size_pct: 0.02
  cooldown_minutes: 30
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            assert "cascade_fade" in registry.get_strategy_names()
            assert registry.get_mode("cascade_fade") == "ACTIVE"
            assert registry.get_config("cascade_fade")["params"]["min_strength"] == 0.5

    def test_empty_yaml_file_handled_gracefully(self):
        """Test that empty YAML file is handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "empty.yaml"
            config_path.write_text("")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            # Should not crash, just log error
            assert len(registry.get_strategy_names()) == 0

    def test_missing_name_field_handled_gracefully(self):
        """Test that YAML without name field is handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "no_name.yaml"
            config_path.write_text("""
mode: ACTIVE
timescale: 15m
params:
  min_probability: 0.55
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            # Should not crash, just log error
            assert len(registry.get_strategy_names()) == 0

    def test_nonexistent_config_dir_handled_gracefully(self):
        """Test handling of nonexistent config directory."""
        registry = StrategyRegistry(config_dir="/nonexistent/path")
        registry.load_all()

        assert len(registry.get_strategy_names()) == 0


class TestStrategyRegistryEvaluation:
    """Integration tests for strategy evaluation."""

    def test_evaluate_active_strategy(self):
        """Test evaluating an active strategy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "trend.yaml"
            config_path.write_text("""
name: regime_trend
mode: ACTIVE
timescale: 15m
params:
  min_probability: 0.55
  size_multiplier: 1.2
  stop_bps: 150
  tp_bps: 200
  hold_minutes: 60
  min_expected_move_bps: 30.0
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            # Create v4 snapshot with strong upward signal
            v4 = V4Snapshot(
                asset="BTC",
                ts=1776400000.0,
                last_price=70000.0,
                consensus=Consensus(
                    safe_to_trade=True,
                    safe_to_trade_reason="ok",
                    reference_price=70000.0,
                    max_divergence_bps=0.5,
                    source_agreement_score=0.98,
                ),
                macro=MacroBias(
                    bias="NEUTRAL",
                    confidence=50,
                    direction_gate="ALLOW_ALL",
                    size_modifier=1.0,
                    status="ok",
                ),
                timescales={
                    "15m": TimescalePayload(
                        timescale="15m",
                        status="ok",
                        probability_up=0.72,
                        regime="TRENDING_UP",
                        expected_move_bps=20.0,
                        window_close_ts=1776400000,
                        quantiles_at_close=Quantiles(
                            p10=69500.0,
                            p25=69700.0,
                            p50=70200.0,
                            p75=70600.0,
                            p90=71000.0,
                        ),
                    )
                },
            )

            results = registry.evaluate(v4)

            # Active strategy should be evaluated
            assert "regime_trend" in results
            decision = results["regime_trend"]
            assert decision is not None

    def test_evaluate_disabled_strategy(self):
        """Test that disabled strategies are not evaluated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "trend.yaml"
            config_path.write_text("""
name: regime_trend
mode: DISABLED
timescale: 15m
params:
  min_probability: 0.55
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            v4 = V4Snapshot(
                asset="BTC",
                ts=1776400000.0,
                last_price=70000.0,
                consensus=Consensus(
                    safe_to_trade=True,
                    safe_to_trade_reason="ok",
                    reference_price=70000.0,
                    max_divergence_bps=0.5,
                    source_agreement_score=0.98,
                ),
                macro=MacroBias(
                    bias="NEUTRAL",
                    confidence=50,
                    direction_gate="ALLOW_ALL",
                    size_modifier=1.0,
                    status="ok",
                ),
                timescales={},
            )

            results = registry.evaluate(v4)

            # Disabled strategy should not be in results
            assert "regime_trend" not in results

    def test_evaluate_multiple_active_strategies(self):
        """Test evaluating multiple active strategies."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Trend strategy
            config1 = Path(tmpdir) / "trend.yaml"
            config1.write_text("""
name: regime_trend
mode: ACTIVE
timescale: 15m
params:
  min_probability: 0.55
""")

            # Mean reversion strategy
            config2 = Path(tmpdir) / "mean_reversion.yaml"
            config2.write_text("""
name: regime_mean_reversion
mode: ACTIVE
timescale: 15m
params:
  entry_threshold: 0.70
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            v4 = V4Snapshot(
                asset="BTC",
                ts=1776400000.0,
                last_price=70000.0,
                consensus=Consensus(
                    safe_to_trade=True,
                    safe_to_trade_reason="ok",
                    reference_price=70000.0,
                    max_divergence_bps=0.5,
                    source_agreement_score=0.98,
                ),
                macro=MacroBias(
                    bias="NEUTRAL",
                    confidence=50,
                    direction_gate="ALLOW_ALL",
                    size_modifier=1.0,
                    status="ok",
                ),
                timescales={
                    "15m": TimescalePayload(
                        timescale="15m",
                        status="ok",
                        probability_up=0.72,
                        regime="TRENDING_UP",
                        expected_move_bps=20.0,
                        window_close_ts=1776400000,
                        quantiles_at_close=Quantiles(
                            p10=69500.0,
                            p25=69700.0,
                            p50=70200.0,
                            p75=70600.0,
                            p90=71000.0,
                        ),
                    )
                },
            )

            results = registry.evaluate(v4)

            # Both active strategies should be evaluated
            assert len(results) == 2
            assert "regime_trend" in results
            assert "regime_mean_reversion" in results


class TestStrategyRegistryHotReload:
    """Integration tests for hot-reload functionality."""

    def test_reload_updates_strategies(self):
        """Test that reload picks up changes to YAML files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "trend.yaml"
            config_path.write_text("""
name: regime_trend
mode: DISABLED
timescale: 15m
params:
  min_probability: 0.55
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            # Initial load - disabled
            assert registry.get_mode("regime_trend") == "DISABLED"

            # Update config
            config_path.write_text("""
name: regime_trend
mode: ACTIVE
timescale: 15m
params:
  min_probability: 0.60
""")

            # Reload
            registry.reload()

            # Should now be active with updated params
            assert registry.get_mode("regime_trend") == "ACTIVE"
            assert (
                registry.get_config("regime_trend")["params"]["min_probability"] == 0.60
            )

    def test_reload_adds_new_strategies(self):
        """Test that reload picks up new YAML files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Initial config
            config1 = Path(tmpdir) / "trend.yaml"
            config1.write_text("""
name: regime_trend
mode: ACTIVE
timescale: 15m
params:
  min_probability: 0.55
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            assert len(registry.get_strategy_names()) == 1

            # Add new config
            config2 = Path(tmpdir) / "mean_reversion.yaml"
            config2.write_text("""
name: regime_mean_reversion
mode: ACTIVE
timescale: 1h
params:
  entry_threshold: 0.70
""")

            # Reload
            registry.reload()

            # Should now have both strategies
            assert len(registry.get_strategy_names()) == 2
            assert "regime_mean_reversion" in registry.get_strategy_names()

    def test_reload_removes_deleted_strategies(self):
        """Test that reload removes deleted YAML files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Initial configs
            config1 = Path(tmpdir) / "trend.yaml"
            config1.write_text("""
name: regime_trend
mode: ACTIVE
timescale: 15m
params:
  min_probability: 0.55
""")

            config2 = Path(tmpdir) / "mean_reversion.yaml"
            config2.write_text("""
name: regime_mean_reversion
mode: ACTIVE
timescale: 1h
params:
  entry_threshold: 0.70
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            assert len(registry.get_strategy_names()) == 2

            # Delete one config
            config2.unlink()

            # Reload
            registry.reload()

            # Should only have trend strategy
            assert len(registry.get_strategy_names()) == 1
            assert "regime_trend" in registry.get_strategy_names()
            assert "regime_mean_reversion" not in registry.get_strategy_names()

    def test_reload_clears_old_state(self):
        """Test that reload properly clears old strategy state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "trend.yaml"
            config_path.write_text("""
name: regime_trend
mode: ACTIVE
timescale: 15m
params:
  min_probability: 0.55
""")

            registry = StrategyRegistry(config_dir=tmpdir)
            registry.load_all()

            # Get initial strategy instance
            initial_strategy = registry.get_strategy("regime_trend")
            assert initial_strategy is not None

            # Update config
            config_path.write_text("""
name: regime_trend
mode: ACTIVE
timescale: 15m
params:
  min_probability: 0.70
""")

            # Reload
            registry.reload()

            # Should get new strategy instance
            new_strategy = registry.get_strategy("regime_trend")
            assert new_strategy is not None
            assert new_strategy is not initial_strategy  # Different instance


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
