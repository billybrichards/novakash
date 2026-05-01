"""Tests for ComputeStrategyComparison use case.

Uses in-memory fake repos to verify aggregation logic without DB.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import pytest

from domain.strategy_comparison.entities import StrategyComparison
from domain.strategy_comparison.value_objects import (
    DirectionFilter,
    RegimeFilter,
    TBand,
    WindowPeriod,
)
from use_cases.compute_strategy_comparison import ComputeStrategyComparison
from use_cases.ports.decisions_query_repo import DecisionFireRow


def _utc(year=2026, month=5, day=1, hour=12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _utc_now() -> datetime:
    """The 'now' used in uc.execute() — 1 hour after default eval timestamps."""
    return _utc(hour=13)


def _row(
    strategy_id: str,
    direction: str = "UP",
    regime: str = "volatile_trend",
    fill_price: float = 0.5,
    stake_usd: float = 10.0,
    actual_outcome: str | None = "UP",
    eval_offset: int | None = 100,  # ~T-200 from close in a 300s window
    evaluated_at: datetime | None = None,
) -> DecisionFireRow:
    return DecisionFireRow(
        strategy_id=strategy_id,
        asset="BTC",
        timeframe="5m",
        window_ts=1_000_000,
        eval_offset=eval_offset,
        direction=direction,
        regime=regime,
        fill_price=fill_price,
        stake_usd=stake_usd,
        actual_outcome=actual_outcome,
        evaluated_at=evaluated_at or _utc(),
    )


class FakeDecisionsQueryRepo:
    """In-memory stub — returns a pre-built list of rows."""

    def __init__(self, rows: list[DecisionFireRow]) -> None:
        self._rows = rows

    async def fires_with_outcomes(
        self, *, since: datetime, until: datetime
    ) -> Sequence[DecisionFireRow]:
        return [r for r in self._rows if since <= r.evaluated_at < until]


class TestComputeStrategyComparison:
    @pytest.mark.asyncio
    async def test_empty_decisions_returns_no_rows(self):
        uc = ComputeStrategyComparison(decisions_query_repo=FakeDecisionsQueryRepo([]))
        result = await uc.execute(now=_utc_now())
        assert list(result) == []

    @pytest.mark.asyncio
    async def test_two_strategies_produce_separate_rows(self):
        rows = [
            _row("v12_lgb_combo", direction="UP", actual_outcome="UP"),
            _row("v12_lgb_combo", direction="UP", actual_outcome="DOWN"),
            _row("v9_basic", direction="DOWN", actual_outcome="DOWN"),
        ]
        uc = ComputeStrategyComparison(decisions_query_repo=FakeDecisionsQueryRepo(rows))
        result = await uc.execute(now=_utc_now())
        strat_ids = {r.strategy_id for r in result}
        assert "v12_lgb_combo" in strat_ids
        assert "v9_basic" in strat_ids

    @pytest.mark.asyncio
    async def test_win_rate_computed_correctly(self):
        rows = [
            _row("v12_lgb_combo", direction="UP", actual_outcome="UP"),
            _row("v12_lgb_combo", direction="UP", actual_outcome="UP"),
            _row("v12_lgb_combo", direction="UP", actual_outcome="DOWN"),
        ]
        uc = ComputeStrategyComparison(decisions_query_repo=FakeDecisionsQueryRepo(rows))
        result = await uc.execute(now=_utc_now())

        # Find the "all" aggregate for v12_lgb_combo in the 24h window
        agg = _find(result, strategy_id="v12_lgb_combo", window_period=WindowPeriod.H24,
                    t_band=TBand.ALL, direction_filter=DirectionFilter.ALL,
                    regime_filter=RegimeFilter.ALL)
        assert agg is not None
        assert agg.metrics.n_fires == 3
        assert agg.metrics.n_wins == 2
        assert agg.metrics.n_losses == 1
        assert abs(agg.metrics.wr_pct - 66.67) < 0.1

    @pytest.mark.asyncio
    async def test_direction_slice_filters_correctly(self):
        rows = [
            _row("v12_lgb_combo", direction="UP", actual_outcome="UP"),
            _row("v12_lgb_combo", direction="DOWN", actual_outcome="UP"),  # loss for DOWN bet
        ]
        uc = ComputeStrategyComparison(decisions_query_repo=FakeDecisionsQueryRepo(rows))
        result = await uc.execute(now=_utc_now())

        up_slice = _find(result, strategy_id="v12_lgb_combo", window_period=WindowPeriod.H24,
                         t_band=TBand.ALL, direction_filter=DirectionFilter.UP,
                         regime_filter=RegimeFilter.ALL)
        assert up_slice is not None
        assert up_slice.metrics.n_fires == 1
        assert up_slice.metrics.n_wins == 1

        down_slice = _find(result, strategy_id="v12_lgb_combo", window_period=WindowPeriod.H24,
                           t_band=TBand.ALL, direction_filter=DirectionFilter.DOWN,
                           regime_filter=RegimeFilter.ALL)
        assert down_slice is not None
        assert down_slice.metrics.n_fires == 1
        assert down_slice.metrics.n_losses == 1

    @pytest.mark.asyncio
    async def test_pending_not_counted_in_win_or_loss(self):
        rows = [
            _row("v12_lgb_combo", actual_outcome="UP"),
            _row("v12_lgb_combo", actual_outcome=None),  # pending
        ]
        uc = ComputeStrategyComparison(decisions_query_repo=FakeDecisionsQueryRepo(rows))
        result = await uc.execute(now=_utc_now())
        agg = _find(result, strategy_id="v12_lgb_combo", window_period=WindowPeriod.H24,
                    t_band=TBand.ALL, direction_filter=DirectionFilter.ALL,
                    regime_filter=RegimeFilter.ALL)
        assert agg is not None
        assert agg.metrics.n_fires == 2
        assert agg.metrics.n_wins == 1
        assert agg.metrics.n_losses == 0
        assert agg.metrics.n_pending == 1

    @pytest.mark.asyncio
    async def test_real_pnl_computed_for_wins_and_losses(self):
        # fill=0.5, stake=10 → win pnl = (0.5)*(20) - 0.072*10 = 10 - 0.72 = 9.28
        # fill=0.5, stake=10 → loss pnl = -10
        rows = [
            _row("v12_lgb_combo", fill_price=0.5, stake_usd=10.0, actual_outcome="UP"),
            _row("v12_lgb_combo", fill_price=0.5, stake_usd=10.0, actual_outcome="DOWN"),
        ]
        uc = ComputeStrategyComparison(decisions_query_repo=FakeDecisionsQueryRepo(rows))
        result = await uc.execute(now=_utc_now())
        agg = _find(result, strategy_id="v12_lgb_combo", window_period=WindowPeriod.H24,
                    t_band=TBand.ALL, direction_filter=DirectionFilter.ALL,
                    regime_filter=RegimeFilter.ALL)
        assert agg is not None
        expected_pnl = 9.28 + (-10.0)  # = -0.72
        assert abs(agg.metrics.real_net_pnl_usd - expected_pnl) < 0.01

    @pytest.mark.asyncio
    async def test_result_includes_all_window_periods(self):
        rows = [_row("v12_lgb_combo")]
        uc = ComputeStrategyComparison(decisions_query_repo=FakeDecisionsQueryRepo(rows))
        result = await uc.execute(now=_utc_now())
        periods = {r.window_period for r in result if r.strategy_id == "v12_lgb_combo"}
        assert WindowPeriod.H24 in periods

    @pytest.mark.asyncio
    async def test_t_band_bucketing(self):
        # eval_offset=100 in a 300s window → seconds-to-close = 300-100=200 → T_181_240
        rows = [_row("v12_lgb_combo", eval_offset=100, actual_outcome="UP")]
        uc = ComputeStrategyComparison(decisions_query_repo=FakeDecisionsQueryRepo(rows))
        result = await uc.execute(now=_utc_now())
        # Should exist in T_181_240 band
        tband_row = _find(result, strategy_id="v12_lgb_combo", window_period=WindowPeriod.H24,
                          t_band=TBand.T_181_240, direction_filter=DirectionFilter.ALL,
                          regime_filter=RegimeFilter.ALL)
        assert tband_row is not None
        assert tband_row.metrics.n_fires == 1


def _find(
    rows: Sequence[StrategyComparison],
    *,
    strategy_id: str,
    window_period: WindowPeriod,
    t_band: TBand,
    direction_filter: DirectionFilter,
    regime_filter: RegimeFilter,
) -> StrategyComparison | None:
    for r in rows:
        if (
            r.strategy_id == strategy_id
            and r.window_period == window_period
            and r.t_band == t_band
            and r.direction_filter == direction_filter
            and r.regime_filter == regime_filter
        ):
            return r
    return None
