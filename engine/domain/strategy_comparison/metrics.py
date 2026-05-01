"""StrategyMetrics value object — immutable bag of computed stats.

Per-cell rollup: one (strategy, period, t_band, direction, regime) tuple.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyMetrics:
    n_fires: int
    n_wins: int
    n_losses: int
    n_pending: int
    wr_pct: float | None
    wilson_low: float | None
    wilson_high: float | None
    avg_fill: float | None
    median_fill: float | None
    avg_stake_usd: float | None
    real_net_pnl_usd: float | None
    real_pnl_per_fire: float | None
    daily_run_rate_usd: float | None

    @classmethod
    def empty(cls) -> "StrategyMetrics":
        """Zero-fires sentinel — all rates undefined."""
        return cls(
            n_fires=0,
            n_wins=0,
            n_losses=0,
            n_pending=0,
            wr_pct=None,
            wilson_low=None,
            wilson_high=None,
            avg_fill=None,
            median_fill=None,
            avg_stake_usd=None,
            real_net_pnl_usd=None,
            real_pnl_per_fire=None,
            daily_run_rate_usd=None,
        )
