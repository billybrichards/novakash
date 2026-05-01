"""Entities for the strategy-comparison domain.

A `StrategyComparison` is one row in the `strategy_comparison` table —
metrics for one (strategy_id, period, t_band, direction, regime) cell at a
given snapshot_at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .metrics import StrategyMetrics
from .value_objects import DirectionFilter, RegimeFilter, TBand, WindowPeriod


@dataclass(frozen=True)
class StrategyComparison:
    snapshot_at: datetime
    strategy_id: str
    asset: str
    timeframe: str
    window_period: WindowPeriod
    t_band: TBand
    direction_filter: DirectionFilter
    regime_filter: RegimeFilter
    metrics: StrategyMetrics
