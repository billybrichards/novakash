"""Unit tests for V10GateStrategy adapter (SP-02)."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

# Set required env vars before any engine imports trigger Settings()
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from domain.value_objects import StrategyContext, StrategyDecision


def _make_ctx(**overrides) -> StrategyContext:
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
    )
    defaults.update(overrides)
    return StrategyContext(**defaults)


class TestV10GateStrategy:
    @pytest.mark.asyncio
    async def test_pipeline_pass_returns_trade(self):
        """When pipeline passes, returns TRADE with direction and cap."""
        from signals.gates import PipelineResult, GateResult

        mock_result = PipelineResult(
            passed=True,
            direction="UP",
            cap=0.60,
            dune_p=0.72,
            gate_results=[GateResult(passed=True, gate_name="test_gate")],
        )

        from adapters.strategies.v10_gate_strategy import V10GateStrategy
        strategy = V10GateStrategy(dune_client=None)

        with patch.object(strategy._pipeline, "evaluate", new_callable=AsyncMock, return_value=mock_result):
            decision = await strategy.evaluate(_make_ctx())

        assert decision.action == "TRADE"
        assert decision.direction == "UP"
        assert decision.entry_cap == 0.60
        assert decision.confidence_score == 0.72
        assert decision.strategy_id == "v10_gate"
        assert decision.strategy_version == "10.5.3"
        assert decision.skip_reason is None
        assert "v10_DUNE_" in decision.entry_reason

    @pytest.mark.asyncio
    async def test_pipeline_fail_returns_skip(self):
        """When pipeline fails, returns SKIP with reason."""
        from signals.gates import PipelineResult, GateResult

        mock_result = PipelineResult(
            passed=False,
            direction=None,
            gate_results=[GateResult(passed=False, gate_name="SourceAgreementGate", reason="no agreement")],
            failed_gate="SourceAgreementGate",
            skip_reason="no agreement",
        )

        from adapters.strategies.v10_gate_strategy import V10GateStrategy
        strategy = V10GateStrategy(dune_client=None)

        with patch.object(strategy._pipeline, "evaluate", new_callable=AsyncMock, return_value=mock_result):
            decision = await strategy.evaluate(_make_ctx())

        assert decision.action == "SKIP"
        assert decision.direction is None
        assert decision.skip_reason == "no agreement"
        assert decision.metadata["failed_gate"] == "SourceAgreementGate"

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        """When pipeline raises, returns ERROR decision."""
        from adapters.strategies.v10_gate_strategy import V10GateStrategy
        strategy = V10GateStrategy(dune_client=None)

        with patch.object(strategy._pipeline, "evaluate", side_effect=RuntimeError("boom")):
            decision = await strategy.evaluate(_make_ctx())

        assert decision.action == "ERROR"
        assert "boom" in decision.skip_reason
        assert decision.strategy_id == "v10_gate"

    @pytest.mark.asyncio
    async def test_high_confidence_classification(self):
        """Dune P > 0.75 -> HIGH confidence."""
        from signals.gates import PipelineResult, GateResult

        mock_result = PipelineResult(
            passed=True,
            direction="DOWN",
            cap=0.55,
            dune_p=0.82,
            gate_results=[GateResult(passed=True, gate_name="test_gate")],
        )

        from adapters.strategies.v10_gate_strategy import V10GateStrategy
        strategy = V10GateStrategy(dune_client=None)

        with patch.object(strategy._pipeline, "evaluate", new_callable=AsyncMock, return_value=mock_result):
            decision = await strategy.evaluate(_make_ctx())

        assert decision.confidence == "HIGH"

    @pytest.mark.asyncio
    async def test_moderate_confidence_classification(self):
        """Dune P <= 0.75 -> MODERATE confidence."""
        from signals.gates import PipelineResult, GateResult

        mock_result = PipelineResult(
            passed=True,
            direction="UP",
            cap=0.60,
            dune_p=0.68,
            gate_results=[GateResult(passed=True, gate_name="test_gate")],
        )

        from adapters.strategies.v10_gate_strategy import V10GateStrategy
        strategy = V10GateStrategy(dune_client=None)

        with patch.object(strategy._pipeline, "evaluate", new_callable=AsyncMock, return_value=mock_result):
            decision = await strategy.evaluate(_make_ctx())

        assert decision.confidence == "MODERATE"

    def test_strategy_properties(self):
        """Strategy ID and version are correct."""
        from adapters.strategies.v10_gate_strategy import V10GateStrategy
        strategy = V10GateStrategy(dune_client=None)

        assert strategy.strategy_id == "v10_gate"
        assert strategy.version == "10.5.3"
