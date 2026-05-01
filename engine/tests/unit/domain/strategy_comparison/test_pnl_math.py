"""Tests for domain.strategy_comparison.pnl_math — canonical P&L formulas."""
from __future__ import annotations

import math

import pytest

from domain.strategy_comparison.pnl_math import (
    POLYMARKET_CRYPTO_FEE_MULT,
    real_pnl_loss,
    real_pnl_win,
    wilson_interval,
)


class TestRealPnlWin:
    def test_standard_fill(self):
        # fill=0.5, stake=10 → shares=20, gross=10, fee=0.72, net=9.28
        result = real_pnl_win(fill_price=0.5, stake_usd=10.0)
        expected = (1 - 0.5) * (10.0 / 0.5) - POLYMARKET_CRYPTO_FEE_MULT * 10.0
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_high_fill_may_be_negative(self):
        # fill=0.99 → very thin payoff minus fee → net negative
        result = real_pnl_win(fill_price=0.99, stake_usd=10.0)
        gross = (1 - 0.99) * (10.0 / 0.99)
        fee = POLYMARKET_CRYPTO_FEE_MULT * 10.0
        assert math.isclose(result, gross - fee, rel_tol=1e-9)
        assert result < 0  # losing money even on a "win" at high fill

    def test_low_fill_large_payoff(self):
        result = real_pnl_win(fill_price=0.1, stake_usd=100.0)
        expected = (0.9) * (100.0 / 0.1) - POLYMARKET_CRYPTO_FEE_MULT * 100.0
        assert math.isclose(result, expected, rel_tol=1e-9)
        assert result > 0

    def test_zero_stake_raises(self):
        with pytest.raises(ValueError, match="stake_usd"):
            real_pnl_win(fill_price=0.5, stake_usd=0.0)

    def test_negative_stake_raises(self):
        with pytest.raises(ValueError, match="stake_usd"):
            real_pnl_win(fill_price=0.5, stake_usd=-1.0)

    def test_zero_fill_raises(self):
        with pytest.raises(ValueError, match="fill_price"):
            real_pnl_win(fill_price=0.0, stake_usd=10.0)

    def test_negative_fill_raises(self):
        with pytest.raises(ValueError, match="fill_price"):
            real_pnl_win(fill_price=-0.1, stake_usd=10.0)

    def test_fill_equal_to_one_raises(self):
        with pytest.raises(ValueError, match="fill_price"):
            real_pnl_win(fill_price=1.0, stake_usd=10.0)

    def test_fill_above_one_raises(self):
        with pytest.raises(ValueError, match="fill_price"):
            real_pnl_win(fill_price=1.1, stake_usd=10.0)


class TestRealPnlLoss:
    def test_standard_loss(self):
        assert real_pnl_loss(stake_usd=5.0) == -5.0

    def test_large_stake(self):
        assert real_pnl_loss(stake_usd=100.0) == -100.0

    def test_zero_stake_raises(self):
        with pytest.raises(ValueError, match="stake_usd"):
            real_pnl_loss(stake_usd=0.0)

    def test_negative_stake_raises(self):
        with pytest.raises(ValueError, match="stake_usd"):
            real_pnl_loss(stake_usd=-1.0)


class TestWilsonInterval:
    def test_zero_n_returns_full_range(self):
        low, high = wilson_interval(wins=0, n=0)
        assert low == 0.0
        assert high == 1.0

    def test_fifty_of_hundred(self):
        low, high = wilson_interval(wins=50, n=100)
        # Expect roughly [0.40, 0.60]
        assert 0.39 < low < 0.42
        assert 0.58 < high < 0.61

    def test_all_wins(self):
        low, high = wilson_interval(wins=100, n=100)
        assert low > 0.9
        assert math.isclose(high, 1.0, abs_tol=1e-9)

    def test_no_wins(self):
        low, high = wilson_interval(wins=0, n=100)
        assert low == 0.0
        assert high < 0.05

    def test_bounds_clamped_to_unit_interval(self):
        # Any result must lie in [0, 1]
        for wins, n in [(0, 1), (1, 1), (5, 10), (99, 100)]:
            low, high = wilson_interval(wins=wins, n=n)
            assert 0.0 <= low <= 1.0
            assert 0.0 <= high <= 1.0
            assert low <= high

    def test_custom_z(self):
        # z=0 → CI collapses to point estimate
        low, high = wilson_interval(wins=50, n=100, z=0.0)
        assert math.isclose(low, high, abs_tol=1e-9)
