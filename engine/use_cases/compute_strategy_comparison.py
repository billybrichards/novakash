"""Use case: ComputeStrategyComparison.

Given a set of decision rows + outcome rows (read via repository ports),
compute one `StrategyComparison` per (strategy, period, t_band, direction,
regime) cell. Pure orchestration — no IO inside this module.

See docs/architecture/2026-05-01-strategy-comparison-system.md.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Sequence

from domain.strategy_comparison.entities import StrategyComparison
from domain.strategy_comparison.metrics import StrategyMetrics
from domain.strategy_comparison.pnl_math import (
    real_pnl_loss,
    real_pnl_win,
    wilson_interval,
)
from domain.strategy_comparison.value_objects import (
    DirectionFilter,
    RegimeFilter,
    TBand,
    WindowPeriod,
    t_band_from_offset,
)
from use_cases.ports.decisions_query_repo import DecisionFireRow, DecisionsQueryRepoPort

_WINDOW_PERIOD_DELTA: dict[WindowPeriod, timedelta] = {
    WindowPeriod.H1: timedelta(hours=1),
    WindowPeriod.H15: timedelta(hours=15),
    WindowPeriod.H24: timedelta(hours=24),
    WindowPeriod.D7: timedelta(days=7),
    WindowPeriod.D30: timedelta(days=30),
}

# Window duration used to convert eval_offset (from-open) to seconds-to-close.
_WINDOW_DURATION_SECONDS = 300


@dataclass
class _CellAccumulator:
    """Mutable bucket for one (strategy, period, t_band, direction, regime) cell."""

    fills: list[float] = field(default_factory=list)
    stakes: list[float] = field(default_factory=list)
    pnls: list[float] = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    pending: int = 0


@dataclass
class ComputeStrategyComparison:
    """Pure use case — receives a query repo, returns a list of rollups.

    Not an asyncio loop — that lives in the scheduler. This is the unit you
    call once to produce one snapshot.
    """

    decisions_query_repo: DecisionsQueryRepoPort

    async def execute(self, *, now: datetime) -> Sequence[StrategyComparison]:
        """Compute one full snapshot of all comparison cells."""
        result: list[StrategyComparison] = []

        for period, delta in _WINDOW_PERIOD_DELTA.items():
            since = now - delta
            rows = await self.decisions_query_repo.fires_with_outcomes(
                since=since, until=now
            )
            if not rows:
                continue

            cells: dict[tuple[str, TBand, str, str], _CellAccumulator] = {}

            for row in rows:
                tband = _tband_for_row(row)
                # Build slices: specific t_band + ALL, specific direction + ALL,
                # specific regime + ALL — and all combinations.
                directions = [row.direction, DirectionFilter.ALL.value]
                regimes = [row.regime or RegimeFilter.ALL.value, RegimeFilter.ALL.value]
                tbands = [tband, TBand.ALL]

                for tb in _dedupe(tbands):
                    for d in _dedupe(directions):
                        for r in _dedupe(regimes):
                            key = (row.strategy_id, tb, d, r)
                            if key not in cells:
                                cells[key] = _CellAccumulator()
                            _accumulate(cells[key], row)

            for (sid, tb, direction, regime), acc in cells.items():
                entity = _build_entity(
                    snapshot_at=now,
                    strategy_id=sid,
                    period=period,
                    t_band=tb,
                    direction=direction,
                    regime=regime,
                    acc=acc,
                    period_hours=delta.total_seconds() / 3600.0,
                )
                result.append(entity)

        return result


def _tband_for_row(row: DecisionFireRow) -> TBand:
    if row.eval_offset is None:
        return TBand.ALL
    seconds_to_close = _WINDOW_DURATION_SECONDS - row.eval_offset
    return t_band_from_offset(seconds_to_close)


def _dedupe(items: list) -> list:
    seen = []
    for x in items:
        if x not in seen:
            seen.append(x)
    return seen


def _accumulate(acc: _CellAccumulator, row: DecisionFireRow) -> None:
    if row.fill_price is not None and row.fill_price > 0:
        acc.fills.append(row.fill_price)
    if row.stake_usd is not None and row.stake_usd > 0:
        acc.stakes.append(row.stake_usd)

    if row.actual_outcome is None:
        acc.pending += 1
        return

    is_win = (row.actual_outcome or "").upper() == (row.direction or "").upper()
    if is_win:
        acc.wins += 1
        if row.fill_price and row.stake_usd and row.fill_price > 0 and row.stake_usd > 0:
            try:
                acc.pnls.append(real_pnl_win(row.fill_price, row.stake_usd))
            except ValueError:
                pass
    else:
        acc.losses += 1
        if row.stake_usd and row.stake_usd > 0:
            try:
                acc.pnls.append(real_pnl_loss(row.stake_usd))
            except ValueError:
                pass


def _build_entity(
    *,
    snapshot_at: datetime,
    strategy_id: str,
    period: WindowPeriod,
    t_band: TBand,
    direction: str,
    regime: str,
    acc: _CellAccumulator,
    period_hours: float,
) -> StrategyComparison:
    n_fires = acc.wins + acc.losses + acc.pending
    n_resolved = acc.wins + acc.losses

    wr_pct: float | None = None
    wilson_low: float | None = None
    wilson_high: float | None = None
    if n_resolved > 0:
        wr_pct = round(acc.wins / n_resolved * 100, 2)
        low, high = wilson_interval(acc.wins, n_resolved)
        wilson_low = round(low * 100, 2)
        wilson_high = round(high * 100, 2)

    avg_fill = statistics.mean(acc.fills) if acc.fills else None
    median_fill = statistics.median(acc.fills) if acc.fills else None
    avg_stake = statistics.mean(acc.stakes) if acc.stakes else None

    real_net = sum(acc.pnls) if acc.pnls else None
    pnl_per_fire: float | None = None
    daily_run_rate: float | None = None
    if real_net is not None and n_fires > 0:
        pnl_per_fire = real_net / n_fires
        fires_per_hour = n_fires / max(period_hours, 1.0)
        daily_run_rate = pnl_per_fire * fires_per_hour * 24.0

    metrics = StrategyMetrics(
        n_fires=n_fires,
        n_wins=acc.wins,
        n_losses=acc.losses,
        n_pending=acc.pending,
        wr_pct=wr_pct,
        wilson_low=wilson_low,
        wilson_high=wilson_high,
        avg_fill=avg_fill,
        median_fill=median_fill,
        avg_stake_usd=avg_stake,
        real_net_pnl_usd=real_net,
        real_pnl_per_fire=pnl_per_fire,
        daily_run_rate_usd=daily_run_rate,
    )

    try:
        dir_filter = DirectionFilter(direction)
    except ValueError:
        dir_filter = DirectionFilter.ALL

    try:
        reg_filter = RegimeFilter(regime)
    except ValueError:
        reg_filter = RegimeFilter.ALL

    return StrategyComparison(
        snapshot_at=snapshot_at,
        strategy_id=strategy_id,
        asset="BTC",
        timeframe="5m",
        window_period=period,
        t_band=t_band,
        direction_filter=dir_filter,
        regime_filter=reg_filter,
        metrics=metrics,
    )
