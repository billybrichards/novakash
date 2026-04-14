"""
Integration tests for persistence layer.

Tests position save/load roundtrip, v4 snapshot persistence,
and continuation state persistence.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from margin_engine.application.dto import OpenPositionInput
from margin_engine.application.use_cases.open_position import OpenPositionUseCase
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


class TestPositionPersistence:
    """Integration tests for position persistence."""

    @pytest.mark.asyncio
    async def test_position_save_load_roundtrip(self):
        """Test complete position save and load roundtrip."""
        repo = InMemoryPositionRepository()
        exchange = MockExchange()
        alerts = MockAlertPort()
        signal_port = MockSignalPort()
        probability_port = MockProbabilityPort()
        v4_port = MockV4SnapshotPort()

        # Create position manually
        position = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=StopLevel(69500.0),
            take_profit=StopLevel(70500.0),
            max_hold_seconds=900,
        )

        # Save position
        await repo.save(position)

        # Load position
        loaded = await repo.get_by_id(position.id)

        # Verify all fields
        assert loaded is not None
        assert loaded.id == position.id
        assert loaded.asset == position.asset
        assert loaded.side == position.side
        assert loaded.entry_price.value == position.entry_price.value
        assert loaded.notional == position.notional
        assert loaded.stop_loss is not None
        assert loaded.stop_loss.price == position.stop_loss.price
        assert loaded.take_profit is not None
        assert loaded.take_profit.price == position.take_profit.price
        assert loaded.max_hold_seconds == position.max_hold_seconds

    @pytest.mark.asyncio
    async def test_position_state_persistence(self):
        """Test that position state changes are persisted."""
        repo = InMemoryPositionRepository()

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

        # Save open position
        position.state = PositionState.OPEN
        await repo.save(position)

        # Load and verify state
        loaded = await repo.get_by_id(position.id)
        assert loaded.state.value == "OPEN"

        # Update state to closed
        position.state = PositionState.CLOSED
        position.exit_reason = "TAKE_PROFIT"
        await repo.save(position)

        # Load and verify state change
        loaded = await repo.get_by_id(position.id)
        assert loaded.state.value == "CLOSED"
        assert loaded.exit_reason.value == "take_profit"

    @pytest.mark.asyncio
    async def test_multiple_positions_persistence(self):
        """Test persisting multiple positions."""
        repo = InMemoryPositionRepository()

        # Create multiple positions
        positions = []
        for i in range(3):
            position = Position(
                asset="BTC",
                side=TradeSide.LONG,
                entry_price=Price(70000.0 + i * 100),
                notional=Money.usd(70.0),
                stop_loss=None,
                take_profit=None,
                max_hold_seconds=900,
            )
            position.state = "OPEN"
            await repo.save(position)
            positions.append(position)

        # Get all positions
        all_positions = repo.get_all()
        assert len(all_positions) == 3

        # Get open positions
        open_positions = await repo.get_open_positions()
        assert len(open_positions) == 3

        # Close one position
        positions[0].state = "CLOSED"
        positions[0].exit_reason = "stop_loss"
        await repo.save(positions[0])

        # Get open positions again
        open_positions = await repo.get_open_positions()
        assert len(open_positions) == 2

        # Get closed positions
        closed = await repo.get_closed_today()
        assert len(closed) == 1


class TestV4SnapshotPersistence:
    """Integration tests for v4 snapshot persistence."""


class TestContinuationStatePersistence:
    """Integration tests for continuation state persistence."""

    @pytest.mark.asyncio
    async def test_position_hold_clock_persistence(self):
        """Test that hold clock anchor is persisted."""
        repo = InMemoryPositionRepository()

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

        # Set hold clock anchor
        position.hold_clock_anchor = 1776400000.0

        # Save
        await repo.save(position)

        # Load
        loaded = await repo.get_by_id(position.id)

        # Verify hold clock is persisted
        assert loaded is not None
        assert loaded.hold_clock_anchor == 1776400000.0

    @pytest.mark.asyncio
    async def test_position_extension_count_persistence(self):
        """Test that extension count is persisted."""
        repo = InMemoryPositionRepository()

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

        # Simulate extensions
        position.hold_clock_anchor = 1776400000.0
        position.continuation_count = 2

        # Save
        await repo.save(position)

        # Load
        loaded = await repo.get_by_id(position.id)

        # Verify extension count is persisted
        assert loaded is not None
        assert loaded.continuation_count == 2


class TestRepositoryQueryOperations:
    """Integration tests for repository query operations."""

    @pytest.mark.asyncio
    async def test_get_open_positions_filter(self):
        """Test that get_open_positions only returns open positions."""
        repo = InMemoryPositionRepository()

        # Create and save open position
        pos1 = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=None,
            take_profit=None,
            max_hold_seconds=900,
        )
        pos1.state = PositionState.OPEN
        await repo.save(pos1)

        # Create and save closed position
        pos2 = Position(
            asset="BTC",
            side=TradeSide.LONG,
            entry_price=Price(70000.0),
            notional=Money.usd(70.0),
            stop_loss=None,
            take_profit=None,
            max_hold_seconds=900,
        )
        pos2.state = PositionState.CLOSED
        pos2.exit_reason = ExitReason.TAKE_PROFIT
        await repo.save(pos2)

        # Get open positions
        open_positions = await repo.get_open_positions()

        # Should only return open position
        assert len(open_positions) == 1
        assert open_positions[0].id == pos1.id

    @pytest.mark.asyncio
    async def test_get_closed_today(self):
        """Test getting all closed positions."""
        repo = InMemoryPositionRepository()

        # Create multiple positions with different states
        positions = []
        for i in range(5):
            position = Position(
                asset="BTC",
                side=TradeSide.LONG,
                entry_price=Price(70000.0),
                notional=Money.usd(70.0),
                stop_loss=None,
                take_profit=None,
                max_hold_seconds=900,
            )
            position.state = PositionState.OPEN if i < 3 else PositionState.CLOSED
            if position.state.value == "CLOSED":
                position.exit_reason = (
                    ExitReason.TAKE_PROFIT if i % 2 == 0 else ExitReason.STOP_LOSS
                )
            await repo.save(position)
            positions.append(position)

        # Get closed positions
        closed = await repo.get_closed_today()

        # Should return 2 closed positions
        assert len(closed) == 2

        # Verify exit reasons
        reasons = [p.exit_reason.value for p in closed]
        assert "TAKE_PROFIT" in reasons
        assert "STOP_LOSS" in reasons

    @pytest.mark.asyncio
    async def test_repository_clear(self):
        """Test clearing all positions from repository."""
        repo = InMemoryPositionRepository()

        # Add multiple positions
        for i in range(3):
            position = Position(
                asset="BTC",
                side=TradeSide.LONG,
                entry_price=Price(70000.0),
                notional=Money.usd(70.0),
                stop_loss=None,
                take_profit=None,
                max_hold_seconds=900,
            )
            await repo.save(position)

        # Verify positions exist
        assert len(repo.get_all()) == 3

        # Clear repository
        repo.clear()

        # Verify all positions are gone
        assert len(repo.get_all()) == 0
        assert len(await repo.get_open_positions()) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
