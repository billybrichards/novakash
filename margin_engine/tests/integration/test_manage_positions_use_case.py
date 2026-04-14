"""
Integration tests for ManagePositionsUseCase.

Tests complete exit flow including:
- Stop loss trigger
- Take profit trigger
- Trailing stop
- Expiry logic
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from margin_engine.application.dto import ManagePositionsInput
from margin_engine.application.use_cases.manage_positions import ManagePositionsUseCase
from margin_engine.domain.entities.position import Position
from margin_engine.domain.entities.position import PositionState
from margin_engine.domain.value_objects import (
    Consensus,
    ExitReason,
    MacroBias,
    Money,
    Price,
    Quantiles,
    StopLevel,
    TimescalePayload,
    TradeSide,
    V4Snapshot,
)

from .conftest import (
    InMemoryPositionRepository,
    MockExchange,
    MockAlertPort,
    MockSignalPort,
    MockProbabilityPort,
    MockV4SnapshotPort,
)


class TestManagePositionsIntegration:
    """Integration tests for full manage positions flow."""

    @pytest.mark.asyncio
    async def test_stop_loss_trigger(
        self,
        setup_full_environment: dict,
        test_v4_snapshot: V4Snapshot,
    ):
        """Test that stop loss is triggered when price drops below SL."""
        env = setup_full_environment

        # Setup entry price at 70000
        env["exchange"].set_price(70000.0)

        # Create position manually with stop loss
        position = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=StopLevel(69500.0),  # ~71 bps stop
            take_profit=None,
            max_hold_seconds=900,
        )
        position.state = PositionState.OPEN
        await env["repository"].save(position)
        env["portfolio"].add_position(position)

        # Move price down to trigger stop loss
        env["exchange"].set_price(69400.0)  # Below stop loss

        # Create use case
        uc = ManagePositionsUseCase(
            input=ManagePositionsInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                trailing_stop_pct=0.003,
                v4_snapshot_port=env["v4_port"],
                probability_port=env["probability_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_continuation_min_conviction=0.10,
                v4_continuation_max=None,
                v4_event_exit_seconds=120,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
            )
        )

        # Execute tick
        result = await uc.tick()

        # Verify position was closed with stop loss
        assert len(result.closed_positions) == 1
        assert result.closed_positions[0].exit_reason.value == "stop_loss"
        assert result.closed_positions[0].state.value == "CLOSED"

        # Verify close order was placed
        close_orders = [
            o for o in env["exchange"].orders if "close" in o.fill_result.order_id
        ]
        assert len(close_orders) == 1

        # Verify alert was sent
        assert any("closed" in alert for alert in env["alerts"].alerts)

    @pytest.mark.asyncio
    async def test_take_profit_trigger(
        self,
        setup_full_environment: dict,
        test_v4_snapshot: V4Snapshot,
    ):
        """Test that take profit is triggered when price reaches TP."""
        env = setup_full_environment

        # Setup entry price at 70000
        env["exchange"].set_price(70000.0)

        # Create position manually with take profit
        position = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=None,
            take_profit=StopLevel(70500.0),  # ~71 bps TP
            max_hold_seconds=900,
        )
        position.state = PositionState.OPEN
        await env["repository"].save(position)
        env["portfolio"].add_position(position)

        # Move price up to trigger take profit
        env["exchange"].set_price(70600.0)  # Above take profit

        # Create use case
        uc = ManagePositionsUseCase(
            input=ManagePositionsInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                trailing_stop_pct=0.003,
                v4_snapshot_port=env["v4_port"],
                probability_port=env["probability_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_continuation_min_conviction=0.10,
                v4_continuation_max=None,
                v4_event_exit_seconds=120,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
            )
        )

        # Execute tick
        result = await uc.tick()

        # Verify position was closed with take profit
        assert len(result.closed_positions) == 1
        assert result.closed_positions[0].exit_reason.value == "take_profit"

        # Verify PnL is positive
        assert result.closed_positions[0].realised_pnl > 0

    @pytest.mark.asyncio
    async def test_trailing_stop_update(
        self,
        setup_full_environment: dict,
    ):
        """Test that trailing stop is updated as price moves in favor."""
        env = setup_full_environment

        # Setup entry price at 70000
        env["exchange"].set_price(70000.0)

        # Create position without explicit stop (trailing will be applied)
        position = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=None,  # No explicit stop
            take_profit=None,
            max_hold_seconds=900,
        )
        position.state = PositionState.OPEN
        await env["repository"].save(position)
        env["portfolio"].add_position(position)

        # Move price up - trailing stop should be set
        env["exchange"].set_price(70300.0)

        uc = ManagePositionsUseCase(
            input=ManagePositionsInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                trailing_stop_pct=0.003,  # 0.3% trail
                v4_snapshot_port=env["v4_port"],
                probability_port=env["probability_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_continuation_min_conviction=0.10,
                v4_continuation_max=None,
                v4_event_exit_seconds=120,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
            )
        )

        # Execute tick - should continue, not close
        result = await uc.tick()

        # Position should still be open (price hasn't pulled back enough)
        assert len(result.closed_positions) == 0
        assert any("continue" in action for action in result.actions_taken)

    @pytest.mark.asyncio
    async def test_trailing_stop_trigger(
        self,
        setup_full_environment: dict,
    ):
        """Test that trailing stop triggers on pullback."""
        env = setup_full_environment

        # Setup entry price at 70000
        env["exchange"].set_price(70000.0)

        # Create position without explicit stop
        position = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=None,
            take_profit=None,
            max_hold_seconds=900,
        )
        position.state = PositionState.OPEN
        await env["repository"].save(position)
        env["portfolio"].add_position(position)

        # Move price up first
        env["exchange"].set_price(70500.0)

        # Tick to establish trailing stop
        uc = ManagePositionsUseCase(
            input=ManagePositionsInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                trailing_stop_pct=0.003,  # 0.3% trail
                v4_snapshot_port=env["v4_port"],
                probability_port=env["probability_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_continuation_min_conviction=0.10,
                v4_continuation_max=None,
                v4_event_exit_seconds=120,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
            )
        )

        await uc.tick()

        # Now move price down to trigger trailing stop
        # 0.3% trail from 70500 = 70288.5, so 70200 should trigger
        env["exchange"].set_price(70200.0)

        # Execute tick - should close on trailing stop
        result = await uc.tick()

        assert len(result.closed_positions) == 1
        assert result.closed_positions[0].exit_reason.value == "trailing_stop"

    @pytest.mark.asyncio
    async def test_expiry_continuation_v4(
        self,
        setup_full_environment: dict,
        test_v4_snapshot: V4Snapshot,
    ):
        """Test that position continues when v4 still supports it."""
        env = setup_full_environment
        env["v4_port"].set_snapshot(test_v4_snapshot)

        # Create expired position (simulated by setting old timestamp)
        position = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=None,
            take_profit=None,
            max_hold_seconds=900,
        )
        # Manually mark as expired by manipulating internal state
        position._hold_clock_anchor = 0  # Force expiry

        position.state = PositionState.OPEN
        await env["repository"].save(position)
        env["portfolio"].add_position(position)

        uc = ManagePositionsUseCase(
            input=ManagePositionsInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                trailing_stop_pct=0.003,
                v4_snapshot_port=env["v4_port"],
                probability_port=env["probability_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_continuation_min_conviction=0.10,
                v4_continuation_max=None,
                v4_event_exit_seconds=120,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
            )
        )

        # Execute tick
        result = await uc.tick()

        # Position should continue (v4 still supports it)
        assert len(result.closed_positions) == 0
        assert any("continue" in action for action in result.actions_taken)

    @pytest.mark.asyncio
    async def test_event_guard_exit(
        self,
        setup_full_environment: dict,
    ):
        """Test that positions are closed when event guard triggers."""
        env = setup_full_environment

        # Setup v4 snapshot with HIGH event
        event_snapshot = V4Snapshot(
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
                    probability_up=0.55,
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
            max_impact_in_window="HIGH",
            minutes_to_next_high_impact=1,  # Within 2 minutes
        )
        env["v4_port"].set_snapshot(event_snapshot)

        # Create position
        position = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=None,
            take_profit=None,
            max_hold_seconds=900,
        )
        position.state = PositionState.OPEN
        await env["repository"].save(position)
        env["portfolio"].add_position(position)

        uc = ManagePositionsUseCase(
            input=ManagePositionsInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                trailing_stop_pct=0.003,
                v4_snapshot_port=env["v4_port"],
                probability_port=env["probability_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_continuation_min_conviction=0.10,
                v4_continuation_max=None,
                v4_event_exit_seconds=120,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
            )
        )

        # Execute tick
        result = await uc.tick()

        # Position should be closed due to event guard
        assert len(result.closed_positions) == 1
        assert result.closed_positions[0].exit_reason.value == "event_guard"


class TestManagePositionsMultiplePositions:
    """Integration tests with multiple open positions."""

    @pytest.mark.asyncio
    async def test_mixed_exit_reasons(
        self,
        setup_full_environment: dict,
    ):
        """Test handling multiple positions with different exit conditions."""
        env = setup_full_environment
        env["exchange"].set_price(70000.0)

        # Position 1: Will hit stop loss
        pos1 = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=StopLevel(69500.0),
            take_profit=None,
            max_hold_seconds=900,
        )
        pos1.state = "OPEN"
        await env["repository"].save(pos1)
        env["portfolio"].add_position(pos1)

        # Position 2: Will hit take profit
        pos2 = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=None,
            take_profit=StopLevel(70500.0),
            max_hold_seconds=900,
        )
        pos2.state = "OPEN"
        await env["repository"].save(pos2)
        env["portfolio"].add_position(pos2)

        # Move price - pos1 hits stop, pos2 hits take profit
        env["exchange"].set_price(69400.0)  # Below pos1 stop
        # Note: This won't trigger pos2 TP since price went down

        uc = ManagePositionsUseCase(
            input=ManagePositionsInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                trailing_stop_pct=0.003,
                v4_snapshot_port=env["v4_port"],
                probability_port=env["probability_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_continuation_min_conviction=0.10,
                v4_continuation_max=None,
                v4_event_exit_seconds=120,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
            )
        )

        # Execute tick
        result = await uc.tick()

        # Only pos1 should be closed (stop loss)
        assert len(result.closed_positions) == 1
        assert result.closed_positions[0].id == pos1.id
        assert result.closed_positions[0].exit_reason.value == "stop_loss"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
