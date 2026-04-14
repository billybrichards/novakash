"""
Integration tests for OpenPositionUseCase.

Tests complete entry flow with in-memory adapters, covering:
- v4 entry path
- v2 fallback path
- position persistence to DB
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from margin_engine.application.dto import OpenPositionInput
from margin_engine.application.use_cases.open_position import OpenPositionUseCase
from margin_engine.domain.value_objects import (
    Consensus,
    MacroBias,
    Money,
    Quantiles,
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


class TestOpenPositionIntegration:
    """Integration tests for full open position flow."""

    @pytest.mark.asyncio
    async def test_v4_entry_full_flow(
        self,
        setup_full_environment: dict,
        test_v4_snapshot: V4Snapshot,
    ):
        """Test complete v4 entry flow with all dependencies."""
        env = setup_full_environment

        # Setup v4 port to return our snapshot
        env["v4_port"].set_snapshot(test_v4_snapshot)

        # Create use case
        uc = OpenPositionUseCase(
            input=OpenPositionInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                probability_port=env["probability_port"],
                signal_port=env["signal_port"],
                v4_snapshot_port=env["v4_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_entry_edge=0.10,
                v4_min_expected_move_bps=15.0,
                v4_allow_mean_reverting=False,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
                v4_macro_advisory_size_mult_on_conflict=0.75,
                v4_allow_no_edge_if_exp_move_bps_gte=None,
                fee_rate_per_side=0.00045,
                bet_fraction=0.02,
                venue="binance",
                strategy_version="v4",
            )
        )

        # Execute
        result = await uc.execute()

        # Verify position was created
        assert result.position is not None
        assert result.reason == "v4_entry"
        assert result.v4_snapshot is not None

        # Verify position was persisted
        saved = await env["repository"].get_by_id(result.position.id)
        assert saved is not None
        assert saved.side == TradeSide.LONG

        # Verify order was placed
        assert len(env["exchange"].orders) == 1
        order = env["exchange"].orders[0]
        assert order.side == TradeSide.LONG

        # Verify alert was sent
        assert len(env["alerts"].alerts) == 1
        assert "opened" in env["alerts"].alerts[0]


    @pytest.mark.asyncio
    async def test_v2_fallback_when_v4_unavailable(
        self,
        setup_full_environment: dict,
    ):
        """Test v2 fallback path when v4 snapshot is unavailable."""
        env = setup_full_environment
        # Don't set v4 snapshot - should trigger fallback

        uc = OpenPositionUseCase(
            input=OpenPositionInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                probability_port=env["probability_port"],
                signal_port=env["signal_port"],
                v4_snapshot_port=env["v4_port"],
                engine_use_v4_actions=True,  # Flag is on
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_entry_edge=0.10,
                v4_min_expected_move_bps=15.0,
                v4_allow_mean_reverting=False,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
                v4_macro_advisory_size_mult_on_conflict=0.75,
                v4_allow_no_edge_if_exp_move_bps_gte=None,
                fee_rate_per_side=0.00045,
                min_conviction=0.20,
                regime_threshold=0.50,
                regime_timescale="1h",
                bet_fraction=0.02,
                stop_loss_pct=0.006,
                take_profit_pct=0.005,
                venue="binance",
                strategy_version="v2-probability",
            )
        )

        result = await uc.execute()

        # Should fallback to v2
        assert result.reason == "v2_entry" or result.reason == "v2_rejected"
        assert result.v4_snapshot is None

    @pytest.mark.asyncio
    async def test_v4_rejected_returns_none_with_reason(
        self,
        setup_full_environment: dict,
    ):
        """Test that v4 rejection returns None position with reason."""
        env = setup_full_environment

        # Setup v4 snapshot with conditions that should cause rejection
        bad_snapshot = V4Snapshot(
            asset="BTC",
            ts=1776400000.0,
            last_price=70000.0,
            consensus=Consensus(
                safe_to_trade=False,
                safe_to_trade_reason="source_agreement_low",
                reference_price=70000.0,
                max_divergence_bps=0.5,
                source_agreement_score=0.50,  # Too low
            ),
            macro=MacroBias(
                bias="NEUTRAL",
                confidence=50,
                direction_gate="ALLOW_ALL",
                size_modifier=1.0,
                status="ok",
            ),
            timescales={},  # Empty timescales
        )
        env["v4_port"].set_snapshot(bad_snapshot)

        uc = OpenPositionUseCase(
            input=OpenPositionInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                probability_port=env["probability_port"],
                signal_port=env["signal_port"],
                v4_snapshot_port=env["v4_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_entry_edge=0.10,
                v4_min_expected_move_bps=15.0,
                v4_allow_mean_reverting=False,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
                v4_macro_advisory_size_mult_on_conflict=0.75,
                v4_allow_no_edge_if_exp_move_bps_gte=None,
                fee_rate_per_side=0.00045,
                bet_fraction=0.02,
                venue="binance",
                strategy_version="v4",
            )
        )

        result = await uc.execute()

        # Should be rejected, not fallback
        assert result.position is None
        assert result.reason == "v4_rejected"
        assert result.v4_snapshot is not None


class TestOpenPositionPersistence:
    """Integration tests for position persistence."""

    @pytest.mark.asyncio
    async def test_position_save_load_roundtrip(
        self,
        setup_full_environment: dict,
        test_v4_snapshot: V4Snapshot,
    ):
        """Test complete position save and load roundtrip."""
        env = setup_full_environment
        env["v4_port"].set_snapshot(test_v4_snapshot)

        uc = OpenPositionUseCase(
            input=OpenPositionInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                probability_port=env["probability_port"],
                signal_port=env["signal_port"],
                v4_snapshot_port=env["v4_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_entry_edge=0.10,
                v4_min_expected_move_bps=15.0,
                v4_allow_mean_reverting=False,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
                v4_macro_advisory_size_mult_on_conflict=0.75,
                v4_allow_no_edge_if_exp_move_bps_gte=None,
                fee_rate_per_side=0.00045,
                bet_fraction=0.02,
                venue="binance",
                strategy_version="v4",
            )
        )

        result = await uc.execute()
        assert result.position is not None

        # Load back from repository
        loaded = await env["repository"].get_by_id(result.position.id)
        assert loaded is not None

        # Verify all fields match
        assert loaded.asset == result.position.asset
        assert loaded.side == result.position.side
        assert loaded.entry_price.value == result.position.entry_price.value
        assert loaded.notional == result.position.notional

    @pytest.mark.asyncio
    async def test_get_open_positions_filter(
        self,
        setup_full_environment: dict,
        test_v4_snapshot: V4Snapshot,
    ):
        """Test that get_open_positions only returns open positions."""
        env = setup_full_environment
        env["v4_port"].set_snapshot(test_v4_snapshot)

        uc = OpenPositionUseCase(
            input=OpenPositionInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                probability_port=env["probability_port"],
                signal_port=env["signal_port"],
                v4_snapshot_port=env["v4_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_entry_edge=0.10,
                v4_min_expected_move_bps=15.0,
                v4_allow_mean_reverting=False,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
                v4_macro_advisory_size_mult_on_conflict=0.75,
                v4_allow_no_edge_if_exp_move_bps_gte=None,
                fee_rate_per_side=0.00045,
                bet_fraction=0.02,
                venue="binance",
                strategy_version="v4",
            )
        )

        # Open first position
        result1 = await uc.execute()
        assert result1.position is not None

        # Simulate closing the position
        result1.position.request_exit("test_exit")
        result1.position.state = "CLOSED"
        await env["repository"].save(result1.position)

        # Open second position
        # Reset v4 snapshot to ensure it's available
        env["v4_port"].set_snapshot(test_v4_snapshot)
        result2 = await uc.execute()
        assert result2.position is not None

        # Get open positions - should only return the second one
        open_positions = await env["repository"].get_open_positions()
        assert len(open_positions) == 1
        assert open_positions[0].id == result2.position.id


class TestOpenPositionWindowDeduplication:
    """Integration tests for window deduplication logic."""

    @pytest.mark.asyncio
    async def test_no_duplicate_entries_same_window(
        self,
        setup_full_environment: dict,
        test_v4_snapshot: V4Snapshot,
    ):
        """Test that multiple calls in same window don't create duplicate positions."""
        env = setup_full_environment
        env["v4_port"].set_snapshot(test_v4_snapshot)

        uc = OpenPositionUseCase(
            input=OpenPositionInput(
                exchange=env["exchange"],
                portfolio=env["portfolio"],
                repository=env["repository"],
                alerts=env["alerts"],
                probability_port=env["probability_port"],
                signal_port=env["signal_port"],
                v4_snapshot_port=env["v4_port"],
                engine_use_v4_actions=True,
                v4_primary_timescale="15m",
                v4_timescales=("15m",),
                v4_entry_edge=0.10,
                v4_min_expected_move_bps=15.0,
                v4_allow_mean_reverting=False,
                v4_macro_mode="advisory",
                v4_macro_hard_veto_confidence_floor=80,
                v4_macro_advisory_size_mult_on_conflict=0.75,
                v4_allow_no_edge_if_exp_move_bps_gte=None,
                fee_rate_per_side=0.00045,
                bet_fraction=0.02,
                venue="binance",
                strategy_version="v4",
            )
        )

        # First call should succeed
        result1 = await uc.execute()
        assert result1.position is not None

        # Second call in same window should be deduped (no new order)
        result2 = await uc.execute()

        # Only one order should have been placed
        assert len(env["exchange"].orders) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
