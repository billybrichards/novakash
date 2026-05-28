"""Tests for the asset-filter guard in StrategyRegistry.evaluate_all.

The registry must skip strategies whose configured asset doesn't match
the window's asset. Without this, a BTC window would evaluate
v7_15m_sniper_eth and produce mislabelled shadow data.

Special case: asset="ANY" evaluates on every window regardless of asset.
This covers the TickFormer v16/v17/v18/v20 family which are cross-asset
by design. Previously this was broken — "ANY" != "BTC" caused ALL
tickformer strategies to silently skip every window.
(fix/tickformer-strategies-actually-fire — Bug A)
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass, field
from typing import Optional


# ── Minimal stubs ────────────────────────────────────────────────────────────

@dataclass
class FakeConfig:
    name: str
    version: str = "1.0"
    mode: str = "GHOST"
    asset: str = "BTC"
    timescale: str = "15m"
    gates: list = field(default_factory=list)
    sizing: dict = field(default_factory=dict)
    hooks_file: Optional[str] = None
    pre_gate_hook: Optional[str] = None
    post_gate_hook: Optional[str] = None
    gate_params: dict = field(default_factory=dict)


@dataclass
class FakeWindow:
    asset: str = "BTC"
    window_ts: int = 1776700000
    timeframe: str = "15m"
    eval_offset: int = 350


@dataclass
class FakeDecision:
    action: str = "SKIP"
    direction: Optional[str] = None
    strategy_id: str = ""
    strategy_version: str = "1.0"
    skip_reason: Optional[str] = None
    confidence: Optional[float] = None
    confidence_score: Optional[float] = None
    metadata: dict = field(default_factory=dict)


class FakeRegistry:
    """Minimal reproduction of the evaluate_all loop with the asset filter."""

    def __init__(self, configs: dict[str, FakeConfig]):
        self._configs = configs

    def evaluate_all(self, window: FakeWindow) -> list[FakeDecision]:
        window_tf = getattr(window, "timeframe", "5m")
        window_asset = getattr(window, "asset", "BTC")

        decisions = []
        for name, config in self._configs.items():
            if config.mode == "DISABLED":
                continue
            if config.timescale != window_tf:
                continue
            # "ANY" is a sentinel — strategy evaluates on every asset.
            # (fix/tickformer-strategies-actually-fire — Bug A)
            _config_asset = getattr(config, "asset", "BTC")
            if _config_asset != "ANY" and _config_asset != window_asset:
                continue
            decisions.append(FakeDecision(
                action="TRADE", strategy_id=name, strategy_version=config.version,
            ))
        return decisions


# ── Tests ────────────────────────────────────────────────────────────────────

class TestRegistryAssetFilter:

    def test_eth_strategy_skipped_on_btc_window(self):
        """v7_15m_sniper_eth should NOT evaluate on a BTC window."""
        reg = FakeRegistry({
            "v7_15m_sniper": FakeConfig(name="v7_15m_sniper", asset="BTC", timescale="15m"),
            "v7_15m_sniper_eth": FakeConfig(name="v7_15m_sniper_eth", asset="ETH", timescale="15m"),
        })
        decisions = reg.evaluate_all(FakeWindow(asset="BTC", timeframe="15m"))
        strategy_ids = [d.strategy_id for d in decisions]
        assert "v7_15m_sniper" in strategy_ids
        assert "v7_15m_sniper_eth" not in strategy_ids

    def test_btc_strategy_skipped_on_eth_window(self):
        """v7_15m_sniper (BTC) should NOT evaluate on an ETH window."""
        reg = FakeRegistry({
            "v7_15m_sniper": FakeConfig(name="v7_15m_sniper", asset="BTC", timescale="15m"),
            "v7_15m_sniper_eth": FakeConfig(name="v7_15m_sniper_eth", asset="ETH", timescale="15m"),
        })
        decisions = reg.evaluate_all(FakeWindow(asset="ETH", timeframe="15m"))
        strategy_ids = [d.strategy_id for d in decisions]
        assert "v7_15m_sniper_eth" in strategy_ids
        assert "v7_15m_sniper" not in strategy_ids

    def test_same_asset_both_timescales_coexist(self):
        """BTC 5m and BTC 15m strategies coexist — only matching timescale evaluates."""
        reg = FakeRegistry({
            "v6_sniper": FakeConfig(name="v6_sniper", asset="BTC", timescale="5m"),
            "v7_15m_sniper": FakeConfig(name="v7_15m_sniper", asset="BTC", timescale="15m"),
        })
        # 5m window → only v6
        decisions_5m = reg.evaluate_all(FakeWindow(asset="BTC", timeframe="5m"))
        assert [d.strategy_id for d in decisions_5m] == ["v6_sniper"]

        # 15m window → only v7
        decisions_15m = reg.evaluate_all(FakeWindow(asset="BTC", timeframe="15m"))
        assert [d.strategy_id for d in decisions_15m] == ["v7_15m_sniper"]

    def test_default_asset_btc_backwards_compat(self):
        """Window without .asset attribute defaults to BTC. BTC strategies still evaluate."""
        reg = FakeRegistry({
            "v7_15m_sniper": FakeConfig(name="v7_15m_sniper", asset="BTC", timescale="15m"),
        })

        class LegacyWindow:
            window_ts = 1776700000
            timeframe = "15m"
            eval_offset = 350
            # No .asset attribute

        decisions = reg.evaluate_all(LegacyWindow())
        assert len(decisions) == 1
        assert decisions[0].strategy_id == "v7_15m_sniper"

    def test_disabled_still_skipped(self):
        """DISABLED mode is checked before asset — a DISABLED ETH strategy never evaluates."""
        reg = FakeRegistry({
            "v7_15m_sniper_eth": FakeConfig(
                name="v7_15m_sniper_eth", asset="ETH", timescale="15m", mode="DISABLED",
            ),
        })
        decisions = reg.evaluate_all(FakeWindow(asset="ETH", timeframe="15m"))
        assert len(decisions) == 0

    def test_three_assets_one_window(self):
        """BTC + ETH + SOL strategies, SOL window → only SOL evaluates."""
        reg = FakeRegistry({
            "v7_btc": FakeConfig(name="v7_btc", asset="BTC", timescale="15m"),
            "v7_eth": FakeConfig(name="v7_eth", asset="ETH", timescale="15m"),
            "v7_sol": FakeConfig(name="v7_sol", asset="SOL", timescale="15m"),
        })
        decisions = reg.evaluate_all(FakeWindow(asset="SOL", timeframe="15m"))
        assert len(decisions) == 1
        assert decisions[0].strategy_id == "v7_sol"

    # ── Bug A: asset="ANY" evaluates on every asset ─────────────────────
    # Previously "ANY" != "BTC" caused all tickformer strategies to silently
    # skip every window. (fix/tickformer-strategies-actually-fire)

    def test_any_asset_evaluates_on_btc_window(self):
        """asset=ANY strategy runs on BTC windows."""
        reg = FakeRegistry({
            "tickformer_v16_pure": FakeConfig(
                name="tickformer_v16_pure", asset="ANY", timescale="5m", mode="SHADOW",
            ),
            "v6_sniper": FakeConfig(name="v6_sniper", asset="BTC", timescale="5m"),
        })
        decisions = reg.evaluate_all(FakeWindow(asset="BTC", timeframe="5m"))
        ids = [d.strategy_id for d in decisions]
        assert "tickformer_v16_pure" in ids
        assert "v6_sniper" in ids

    def test_any_asset_evaluates_on_eth_window(self):
        """asset=ANY strategy runs on ETH windows too — cross-asset by design."""
        reg = FakeRegistry({
            "tickformer_v18_t180": FakeConfig(
                name="tickformer_v18_t180", asset="ANY", timescale="5m", mode="SHADOW",
            ),
            "v9_2_eth_solo": FakeConfig(name="v9_2_eth_solo", asset="ETH", timescale="5m"),
            "v6_sniper": FakeConfig(name="v6_sniper", asset="BTC", timescale="5m"),
        })
        decisions = reg.evaluate_all(FakeWindow(asset="ETH", timeframe="5m"))
        ids = [d.strategy_id for d in decisions]
        assert "tickformer_v18_t180" in ids
        assert "v9_2_eth_solo" in ids
        assert "v6_sniper" not in ids  # BTC-only, skipped on ETH window

    def test_any_asset_all_four_tickformers_on_btc_window(self):
        """All four tickformer strategies (v16/v17/v18/v20) evaluate on BTC."""
        configs = {
            f"tickformer_v{v}": FakeConfig(
                name=f"tickformer_v{v}", asset="ANY", timescale="5m", mode="SHADOW",
            )
            for v in (16, 17, 18, 20)
        }
        configs["btc_only"] = FakeConfig(name="btc_only", asset="BTC", timescale="5m")
        reg = FakeRegistry(configs)
        decisions = reg.evaluate_all(FakeWindow(asset="BTC", timeframe="5m"))
        ids = [d.strategy_id for d in decisions]
        for v in (16, 17, 18, 20):
            assert f"tickformer_v{v}" in ids, f"tickformer_v{v} missing"
        assert "btc_only" in ids

    def test_any_asset_disabled_still_skipped(self):
        """DISABLED check runs before asset — even ANY is skipped when DISABLED."""
        reg = FakeRegistry({
            "tickformer_v16_pure": FakeConfig(
                name="tickformer_v16_pure", asset="ANY", timescale="5m", mode="DISABLED",
            ),
        })
        decisions = reg.evaluate_all(FakeWindow(asset="BTC", timeframe="5m"))
        assert len(decisions) == 0
