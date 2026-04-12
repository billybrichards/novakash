"""Unit tests for EvaluateStrategiesUseCase (SP-04)."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

# Set required env vars before any engine imports trigger Settings()
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from engine.domain.value_objects import (
    StrategyContext,
    StrategyDecision,
    StrategyDecisionRecord,
    StrategyRegistration,
    EvaluateStrategiesResult,
)


def _make_window(asset="BTC", window_ts=1712345600, eval_offset=120):
    """Build a mock window object."""
    w = MagicMock()
    w.asset = asset
    w.window_ts = window_ts
    w.eval_offset = eval_offset
    w.open_price = 84000.0
    w.up_price = 0.55
    w.down_price = 0.45
    w.duration_secs = 300
    return w


def _make_state(btc_price=84100.0):
    """Build a mock state object."""
    s = MagicMock()
    s.btc_price = btc_price
    return s


def _make_decision(strategy_id="test", action="TRADE", direction="UP", mode="LIVE"):
    """Build a StrategyDecision."""
    return StrategyDecision(
        action=action,
        direction=direction if action == "TRADE" else None,
        confidence="HIGH" if action == "TRADE" else None,
        confidence_score=0.72 if action == "TRADE" else None,
        entry_cap=0.60 if action == "TRADE" else None,
        collateral_pct=None,
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        entry_reason=f"{strategy_id}_reason" if action == "TRADE" else "",
        skip_reason=None if action == "TRADE" else "some_reason",
    )


class _MockStrategy:
    """Mock strategy for testing."""
    def __init__(self, strategy_id, version, decision):
        self._strategy_id = strategy_id
        self._version = version
        self._decision = decision

    @property
    def strategy_id(self):
        return self._strategy_id

    @property
    def version(self):
        return self._version

    async def evaluate(self, ctx):
        return self._decision


class _SlowStrategy(_MockStrategy):
    """Strategy that takes too long."""
    async def evaluate(self, ctx):
        await asyncio.sleep(10)
        return self._decision


class _ErrorStrategy(_MockStrategy):
    """Strategy that raises."""
    async def evaluate(self, ctx):
        raise RuntimeError("strategy exploded")


def _make_uc(strategies, **kw):
    """Build an EvaluateStrategiesUseCase with mocks."""
    from use_cases.evaluate_strategies import EvaluateStrategiesUseCase
    defaults = dict(
        strategies=strategies,
        decision_repo=AsyncMock(),
        vpin_calculator=MagicMock(current_vpin=0.55, regime="NORMAL"),
        db_client=None,
    )
    defaults.update(kw)
    return EvaluateStrategiesUseCase(**defaults)


class TestEvaluateStrategiesUseCase:
    @pytest.mark.asyncio
    async def test_live_strategy_trade_returned(self):
        """LIVE strategy with TRADE action is returned as live_decision."""
        live_decision = _make_decision("v10_gate", "TRADE", "UP")
        live_strat = _MockStrategy("v10_gate", "10.5.3", live_decision)
        live_reg = StrategyRegistration("v10_gate", mode="LIVE", enabled=True, priority=1)

        uc = _make_uc([(live_reg, live_strat)])
        result = await uc.execute(_make_window(), _make_state())

        assert result.live_decision is not None
        assert result.live_decision.action == "TRADE"
        assert result.live_decision.direction == "UP"
        assert result.live_decision.strategy_id == "v10_gate"
        assert not result.already_traded

    @pytest.mark.asyncio
    async def test_ghost_not_returned_as_live(self):
        """GHOST strategy's TRADE is recorded but NOT returned as live_decision."""
        ghost_decision = _make_decision("v4_fusion", "TRADE", "DOWN")
        ghost_strat = _MockStrategy("v4_fusion", "4.0.0", ghost_decision)
        ghost_reg = StrategyRegistration("v4_fusion", mode="GHOST", enabled=True, priority=2)

        live_decision = _make_decision("v10_gate", "SKIP")
        live_strat = _MockStrategy("v10_gate", "10.5.3", live_decision)
        live_reg = StrategyRegistration("v10_gate", mode="LIVE", enabled=True, priority=1)

        uc = _make_uc([(live_reg, live_strat), (ghost_reg, ghost_strat)])
        result = await uc.execute(_make_window(), _make_state())

        # LIVE skipped -> live_decision is None
        assert result.live_decision is None
        # Both strategies evaluated
        assert len(result.all_decisions) == 2

    @pytest.mark.asyncio
    async def test_both_strategies_recorded(self):
        """All decisions (LIVE + GHOST) are recorded to the repo."""
        live_decision = _make_decision("v10_gate", "TRADE", "UP")
        ghost_decision = _make_decision("v4_fusion", "SKIP")

        live_strat = _MockStrategy("v10_gate", "10.5.3", live_decision)
        ghost_strat = _MockStrategy("v4_fusion", "4.0.0", ghost_decision)

        live_reg = StrategyRegistration("v10_gate", mode="LIVE", enabled=True, priority=1)
        ghost_reg = StrategyRegistration("v4_fusion", mode="GHOST", enabled=True, priority=2)

        mock_repo = AsyncMock()
        uc = _make_uc(
            [(live_reg, live_strat), (ghost_reg, ghost_strat)],
            decision_repo=mock_repo,
        )
        result = await uc.execute(_make_window(), _make_state())

        # Give fire-and-forget tasks a moment to complete
        await asyncio.sleep(0.1)

        assert len(result.all_decisions) == 2
        # Repo should have been called for both
        assert mock_repo.write_decision.call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        """Strategy that exceeds timeout gets ERROR decision."""
        slow_decision = _make_decision("slow", "TRADE", "UP")
        slow_strat = _SlowStrategy("slow", "1.0.0", slow_decision)
        slow_reg = StrategyRegistration("slow", mode="LIVE", enabled=True, priority=1)

        uc = _make_uc([(slow_reg, slow_strat)])

        # Patch timeout to be very short
        with patch("use_cases.evaluate_strategies._STRATEGY_TIMEOUT_S", 0.1):
            result = await uc.execute(_make_window(), _make_state())

        assert result.live_decision is None
        assert len(result.all_decisions) == 1
        assert result.all_decisions[0].action == "ERROR"
        assert "timeout" in result.all_decisions[0].skip_reason

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        """Strategy that raises gets ERROR decision."""
        error_strat = _ErrorStrategy("broken", "1.0.0", None)
        error_reg = StrategyRegistration("broken", mode="LIVE", enabled=True, priority=1)

        uc = _make_uc([(error_reg, error_strat)])
        result = await uc.execute(_make_window(), _make_state())

        assert result.live_decision is None
        assert len(result.all_decisions) == 1
        assert result.all_decisions[0].action == "ERROR"
        assert "strategy exploded" in result.all_decisions[0].skip_reason

    @pytest.mark.asyncio
    async def test_disabled_strategy_not_evaluated(self):
        """Disabled strategies are skipped entirely."""
        decision = _make_decision("disabled", "TRADE", "UP")
        strat = _MockStrategy("disabled", "1.0.0", decision)
        reg = StrategyRegistration("disabled", mode="LIVE", enabled=False, priority=1)

        uc = _make_uc([(reg, strat)])
        result = await uc.execute(_make_window(), _make_state())

        assert result.live_decision is None
        assert len(result.all_decisions) == 0

    @pytest.mark.asyncio
    async def test_context_has_basic_fields(self):
        """The built context has the expected fields from window/state."""
        live_decision = _make_decision("v10_gate", "SKIP")
        live_strat = _MockStrategy("v10_gate", "10.5.3", live_decision)
        live_reg = StrategyRegistration("v10_gate", mode="LIVE", enabled=True, priority=1)

        uc = _make_uc([(live_reg, live_strat)])
        result = await uc.execute(_make_window(), _make_state())

        ctx = result.context
        assert ctx.asset == "BTC"
        assert ctx.window_ts == 1712345600
        assert ctx.eval_offset == 120
        assert ctx.current_price == 84100.0
        assert ctx.vpin == 0.55
