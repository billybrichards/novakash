"""Tests for domain.strategy_comparison.metrics.StrategyMetrics."""
from __future__ import annotations

import pytest

from domain.strategy_comparison.metrics import StrategyMetrics


class TestStrategyMetricsEmpty:
    def test_zero_fires(self):
        m = StrategyMetrics.empty()
        assert m.n_fires == 0
        assert m.n_wins == 0
        assert m.n_losses == 0
        assert m.n_pending == 0

    def test_all_rates_none(self):
        m = StrategyMetrics.empty()
        assert m.wr_pct is None
        assert m.wilson_low is None
        assert m.wilson_high is None
        assert m.avg_fill is None
        assert m.median_fill is None
        assert m.avg_stake_usd is None
        assert m.real_net_pnl_usd is None
        assert m.real_pnl_per_fire is None
        assert m.daily_run_rate_usd is None

    def test_frozen(self):
        m = StrategyMetrics.empty()
        with pytest.raises((AttributeError, TypeError)):
            m.n_fires = 1  # type: ignore[misc]
