"""Unit tests for V4FusionStrategy adapter (SP-03)."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

from domain.value_objects import StrategyContext, StrategyDecision, V4Snapshot


def _make_snapshot(**overrides) -> V4Snapshot:
    """Build a V4Snapshot with sensible defaults."""
    defaults = dict(
        probability_up=0.68,
        conviction="MEDIUM",
        conviction_score=0.72,
        regime="calm_trend",
        regime_confidence=0.85,
        regime_persistence=0.9,
        regime_transition=None,
        recommended_side="UP",
        recommended_collateral_pct=0.03,
        recommended_sl_pct=0.05,
        recommended_tp_pct=0.10,
        recommended_reason="strong trend",
        recommended_conviction_score=0.72,
        sub_signals={"trend": 0.8, "vol": 0.3},
        consensus={"safe_to_trade": True, "sources_agree": 4},
        macro={"direction_gate": "UP", "size_modifier": 1.0},
        quantiles={"p10": -0.02, "p50": 0.01, "p90": 0.04},
        timescale="5m",
        timestamp=1712345600.0,
    )
    defaults.update(overrides)
    return V4Snapshot(**defaults)


def _make_ctx(v4_snapshot=None, **overrides) -> StrategyContext:
    """Build a minimal StrategyContext for testing."""
    defaults = dict(
        asset="BTC",
        window_ts=1712345600,
        timeframe="5m",
        eval_offset=120,
        delta_chainlink=0.0005,
        delta_tiingo=0.0006,
        delta_binance=0.0004,
        delta_pct=0.0005,
        delta_source="tiingo_rest_candle",
        current_price=84000.0,
        open_price=83950.0,
        vpin=0.55,
        regime="NORMAL",
        cg_snapshot=None,
        twap_delta=0.0003,
        tiingo_close=84050.0,
        gamma_up_price=0.55,
        gamma_down_price=0.45,
        clob_up_bid=0.52,
        clob_up_ask=0.54,
        clob_down_bid=0.44,
        clob_down_ask=0.46,
        v4_snapshot=v4_snapshot,
    )
    defaults.update(overrides)
    return StrategyContext(**defaults)


class TestV4FusionStrategy:
    @pytest.mark.asyncio
    async def test_trade_on_valid_snapshot(self):
        """All gates pass -> TRADE."""
        from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
        strategy = V4FusionStrategy()

        snap = _make_snapshot()
        decision = await strategy.evaluate(_make_ctx(v4_snapshot=snap))

        assert decision.action == "TRADE"
        assert decision.direction == "UP"
        assert decision.confidence == "MEDIUM"
        assert decision.confidence_score == 0.72
        assert decision.strategy_id == "v4_fusion"
        assert decision.skip_reason is None
        assert "v4_MEDIUM_calm_trend" in decision.entry_reason

    @pytest.mark.asyncio
    async def test_error_on_none_snapshot(self):
        """No V4 snapshot -> ERROR."""
        from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
        strategy = V4FusionStrategy()

        decision = await strategy.evaluate(_make_ctx(v4_snapshot=None))

        assert decision.action == "ERROR"
        assert "v4_snapshot_missing" in decision.skip_reason

    @pytest.mark.asyncio
    async def test_skip_on_bad_regime(self):
        """Regime not in tradeable set -> SKIP."""
        from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
        strategy = V4FusionStrategy()

        snap = _make_snapshot(regime="chop")
        decision = await strategy.evaluate(_make_ctx(v4_snapshot=snap))

        assert decision.action == "SKIP"
        assert "regime=chop not tradeable" in decision.skip_reason

    @pytest.mark.asyncio
    async def test_skip_on_unsafe_consensus(self):
        """Consensus not safe -> SKIP."""
        from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
        strategy = V4FusionStrategy()

        snap = _make_snapshot(consensus={"safe_to_trade": False})
        decision = await strategy.evaluate(_make_ctx(v4_snapshot=snap))

        assert decision.action == "SKIP"
        assert "consensus not safe_to_trade" in decision.skip_reason

    @pytest.mark.asyncio
    async def test_skip_on_low_conviction(self):
        """Conviction too low for the threshold -> SKIP."""
        from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
        strategy = V4FusionStrategy()

        # MEDIUM requires distance >= 0.15, p_up=0.55 gives distance=0.05
        snap = _make_snapshot(probability_up=0.55, conviction="MEDIUM")
        decision = await strategy.evaluate(_make_ctx(v4_snapshot=snap))

        assert decision.action == "SKIP"
        assert "conviction=MEDIUM" in decision.skip_reason
        assert "distance" in decision.skip_reason

    @pytest.mark.asyncio
    async def test_skip_on_macro_direction_mismatch(self):
        """Macro gate disagrees with direction -> SKIP."""
        from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
        strategy = V4FusionStrategy()

        # p_up=0.68 -> direction UP, but macro says DOWN
        snap = _make_snapshot(
            probability_up=0.68,
            macro={"direction_gate": "DOWN", "size_modifier": 1.0},
        )
        decision = await strategy.evaluate(_make_ctx(v4_snapshot=snap))

        assert decision.action == "SKIP"
        assert "macro direction_gate=DOWN vs UP" in decision.skip_reason

    @pytest.mark.asyncio
    async def test_direction_inferred_from_probability(self):
        """When recommended_side is None, direction inferred from p_up."""
        from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
        strategy = V4FusionStrategy()

        snap = _make_snapshot(
            probability_up=0.30,
            conviction="HIGH",
            recommended_side=None,
            macro={"direction_gate": "DOWN", "size_modifier": 1.0},
        )
        decision = await strategy.evaluate(_make_ctx(v4_snapshot=snap))

        assert decision.action == "TRADE"
        assert decision.direction == "DOWN"

    @pytest.mark.asyncio
    async def test_collateral_scaled_by_size_modifier(self):
        """Collateral pct is scaled by macro.size_modifier."""
        from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
        strategy = V4FusionStrategy()

        snap = _make_snapshot(
            recommended_collateral_pct=0.05,
            macro={"direction_gate": "UP", "size_modifier": 0.5},
        )
        decision = await strategy.evaluate(_make_ctx(v4_snapshot=snap))

        assert decision.action == "TRADE"
        assert abs(decision.collateral_pct - 0.025) < 1e-9

    @pytest.mark.asyncio
    async def test_none_conviction_never_trades(self):
        """Conviction NONE has threshold 1.0 -- never trades."""
        from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
        strategy = V4FusionStrategy()

        snap = _make_snapshot(
            probability_up=0.99,
            conviction="NONE",
        )
        decision = await strategy.evaluate(_make_ctx(v4_snapshot=snap))

        assert decision.action == "SKIP"

    def test_strategy_properties(self):
        """Strategy ID and version are correct."""
        from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
        strategy = V4FusionStrategy()

        assert strategy.strategy_id == "v4_fusion"
        assert strategy.version == "4.0.0"
