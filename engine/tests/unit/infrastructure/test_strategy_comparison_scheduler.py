"""Tests for strategy_comparison_loop scheduler.

Verifies it invokes the use case on cadence and is crash-isolated.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.strategy_comparison.entities import StrategyComparison
from domain.strategy_comparison.metrics import StrategyMetrics
from domain.strategy_comparison.value_objects import (
    DirectionFilter,
    RegimeFilter,
    TBand,
    WindowPeriod,
)
from infrastructure.schedulers.strategy_comparison_scheduler import (
    strategy_comparison_loop,
)


def _make_snapshot() -> list[StrategyComparison]:
    return [
        StrategyComparison(
            snapshot_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            strategy_id="v12_lgb_combo",
            asset="BTC",
            timeframe="5m",
            window_period=WindowPeriod.H24,
            t_band=TBand.ALL,
            direction_filter=DirectionFilter.ALL,
            regime_filter=RegimeFilter.ALL,
            metrics=StrategyMetrics.empty(),
        )
    ]


class TestStrategyComparisonScheduler:
    @pytest.mark.asyncio
    async def test_loop_calls_use_case_and_repo(self):
        """One tick: use case called, repo.save called with result."""
        snapshot = _make_snapshot()
        use_case = MagicMock()
        use_case.execute = AsyncMock(return_value=snapshot)
        repo = MagicMock()
        repo.save = AsyncMock(return_value=1)
        repo.prune_older_than_days = AsyncMock(return_value=0)

        tick_count = 0

        async def _fake_sleep(n):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                raise asyncio.CancelledError()

        task = asyncio.create_task(
            strategy_comparison_loop(
                use_case=use_case,
                repo=repo,
                interval=0,
                prune_every_ticks=999,
            )
        )

        # Patch asyncio.sleep inside the scheduler module
        import infrastructure.schedulers.strategy_comparison_scheduler as sched_mod
        original_sleep = asyncio.sleep
        sched_mod._asyncio_sleep = _fake_sleep

        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        sched_mod._asyncio_sleep = original_sleep
        assert use_case.execute.called
        assert repo.save.called

    @pytest.mark.asyncio
    async def test_loop_survives_use_case_exception(self):
        """A raised exception from use_case must NOT crash the loop."""
        call_count = 0
        boom_then_ok = []

        async def _fake_execute(*, now):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated failure")
            return _make_snapshot()

        use_case = MagicMock()
        use_case.execute = _fake_execute
        repo = MagicMock()
        repo.save = AsyncMock(return_value=1)
        repo.prune_older_than_days = AsyncMock(return_value=0)

        ticks = 0

        async def _stop_after_two(n):
            nonlocal ticks
            ticks += 1
            if ticks >= 2:
                raise asyncio.CancelledError()

        import infrastructure.schedulers.strategy_comparison_scheduler as sched_mod
        original = sched_mod._asyncio_sleep
        sched_mod._asyncio_sleep = _stop_after_two

        task = asyncio.create_task(
            strategy_comparison_loop(
                use_case=use_case,
                repo=repo,
                interval=0,
                prune_every_ticks=999,
            )
        )
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        sched_mod._asyncio_sleep = original
        assert call_count >= 2, "Loop should continue after exception"
        assert repo.save.called
