"""
Tests for the Risk Manager.

Tests cover all 7 risk gates:
  1. Kill switch (45% drawdown)
  2. Daily loss limit (10%)
  3. Position limit (2.5% per bet)
  4. Exposure limit (30% open simultaneously)
  5. Cooldown (3 consecutive losses → 15min pause)
  6. Venue connectivity
  7. Paper mode

Plus: approval path, daily reset, bankroll updates.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution.risk_manager import RiskManager
from execution.order_manager import OrderManager, Order, OrderStatus
from config.constants import (
    MAX_DRAWDOWN_KILL,
    BET_FRACTION,
    MAX_OPEN_EXPOSURE_PCT,
    DAILY_LOSS_LIMIT_PCT,
    CONSECUTIVE_LOSS_COOLDOWN,
)


BANKROLL = 10_000.0  # $10k starting bankroll for tests


@pytest.fixture
def mock_order_manager() -> AsyncMock:
    """Mock OrderManager that returns zero open exposure by default."""
    om = AsyncMock(spec=OrderManager)
    om.get_open_exposure_usd.return_value = 0.0
    return om


@pytest.fixture
def risk(mock_order_manager: AsyncMock) -> RiskManager:
    """Standard RiskManager with $10k bankroll."""
    return RiskManager(
        order_manager=mock_order_manager,
        starting_bankroll=BANKROLL,
        paper_mode=False,
    )


@pytest.fixture
def paper_risk(mock_order_manager: AsyncMock) -> RiskManager:
    """RiskManager in paper mode."""
    return RiskManager(
        order_manager=mock_order_manager,
        starting_bankroll=BANKROLL,
        paper_mode=True,
    )


class TestKillSwitch:
    """Gate 1: Kill switch at 45% drawdown from peak."""

    @pytest.mark.asyncio
    async def test_blocks_at_45pct_drawdown(self, risk: RiskManager) -> None:
        """Loss of 45%+ from peak should trigger kill switch."""
        # Simulate 45% drawdown
        loss = BANKROLL * MAX_DRAWDOWN_KILL
        risk._current_bankroll = BANKROLL - loss

        approved, reason = await risk.approve(100.0)
        assert not approved
        assert "kill_switch" in reason

    @pytest.mark.asyncio
    async def test_allows_trade_below_kill_threshold(self, risk: RiskManager) -> None:
        """Loss of 44% should NOT trigger kill switch."""
        loss = BANKROLL * 0.44
        risk._current_bankroll = BANKROLL - loss

        approved, reason = await risk.approve(100.0)
        # May still fail other gates, but not kill switch
        assert "kill_switch" not in reason

    @pytest.mark.asyncio
    async def test_kill_switch_respects_peak(self, risk: RiskManager) -> None:
        """Kill switch is measured from peak, not starting bankroll."""
        # Bankroll grew then crashed
        risk._peak_bankroll = 15_000.0
        risk._current_bankroll = 8_000.0  # 46.7% drawdown from peak

        approved, reason = await risk.approve(100.0)
        assert not approved
        assert "kill_switch" in reason


class TestDailyLossLimit:
    """Gate 2: Daily loss limit at 10% of day-start bankroll."""

    @pytest.mark.asyncio
    async def test_blocks_when_daily_loss_exceeded(self, risk: RiskManager) -> None:
        """10%+ daily loss → reject trade."""
        max_daily_loss = BANKROLL * DAILY_LOSS_LIMIT_PCT
        risk._daily_pnl = -max_daily_loss

        approved, reason = await risk.approve(100.0)
        assert not approved
        assert "daily_loss" in reason

    @pytest.mark.asyncio
    async def test_allows_trade_before_daily_limit(self, risk: RiskManager) -> None:
        """9% daily loss should NOT trigger daily limit."""
        risk._daily_pnl = -(BANKROLL * 0.09)

        approved, reason = await risk.approve(100.0)
        assert "daily_loss" not in reason


class TestPositionLimit:
    """Gate 3: Per-trade position limit at 2.5% of bankroll."""

    @pytest.mark.asyncio
    async def test_blocks_oversized_position(self, risk: RiskManager) -> None:
        """Stake > 2.5% bankroll → reject."""
        oversized_stake = BANKROLL * BET_FRACTION + 1.0  # Just over limit

        approved, reason = await risk.approve(oversized_stake)
        assert not approved
        assert "position_limit" in reason

    @pytest.mark.asyncio
    async def test_allows_correctly_sized_position(self, risk: RiskManager) -> None:
        """Stake = exactly 2.5% bankroll → allowed."""
        max_stake = BANKROLL * BET_FRACTION

        approved, reason = await risk.approve(max_stake)
        assert "position_limit" not in reason


class TestExposureLimit:
    """Gate 4: Total open exposure limit at 30% of bankroll."""

    @pytest.mark.asyncio
    async def test_blocks_when_exposure_exceeded(self, mock_order_manager: AsyncMock, risk: RiskManager) -> None:
        """New stake pushing total exposure over 30% → reject."""
        # Already at 28% open exposure
        mock_order_manager.get_open_exposure_usd.return_value = BANKROLL * 0.28

        # New $250 stake would push over 30%
        new_stake = BANKROLL * 0.025  # 2.5%

        approved, reason = await risk.approve(new_stake)
        assert not approved
        assert "exposure_limit" in reason

    @pytest.mark.asyncio
    async def test_allows_within_exposure_limit(self, mock_order_manager: AsyncMock, risk: RiskManager) -> None:
        """Low existing exposure → new position allowed."""
        mock_order_manager.get_open_exposure_usd.return_value = BANKROLL * 0.10  # 10%

        approved, reason = await risk.approve(BANKROLL * 0.025)
        assert "exposure_limit" not in reason


class TestCooldown:
    """Gate 5: Cooldown after 3 consecutive losses."""

    @pytest.mark.asyncio
    async def test_cooldown_triggers_after_consecutive_losses(self, risk: RiskManager) -> None:
        """3 consecutive losses → trade blocked for 15 minutes."""
        for _ in range(CONSECUTIVE_LOSS_COOLDOWN):
            await risk.record_outcome(-100.0)

        approved, reason = await risk.approve(100.0)
        assert not approved
        assert "cooldown" in reason

    @pytest.mark.asyncio
    async def test_win_resets_consecutive_loss_counter(self, risk: RiskManager) -> None:
        """A win mid-streak resets the consecutive loss counter."""
        await risk.record_outcome(-100.0)
        await risk.record_outcome(-100.0)
        await risk.record_outcome(+500.0)  # win resets counter
        await risk.record_outcome(-100.0)  # back to 1 loss, no cooldown

        approved, reason = await risk.approve(100.0)
        assert "cooldown" not in reason

    @pytest.mark.asyncio
    async def test_cooldown_expires(self, risk: RiskManager) -> None:
        """After cooldown time passes, trades are allowed again."""
        # Manually set expired cooldown
        risk._cooldown_until = datetime.utcnow() - timedelta(seconds=1)
        risk._consecutive_losses = CONSECUTIVE_LOSS_COOLDOWN

        approved, reason = await risk.approve(100.0)
        assert "cooldown" not in reason


class TestVenueConnectivity:
    """Gate 6: At least one execution venue must be reachable."""

    @pytest.mark.asyncio
    async def test_blocks_when_both_venues_offline(self, risk: RiskManager) -> None:
        """Both venues offline → reject all trades."""
        await risk.update_venue_status(polymarket=False, opinion=False)

        approved, reason = await risk.approve(100.0)
        assert not approved
        assert "venue_connectivity" in reason

    @pytest.mark.asyncio
    async def test_allows_with_polymarket_online(self, risk: RiskManager) -> None:
        """Polymarket online → allowed (even if Opinion offline)."""
        await risk.update_venue_status(polymarket=True, opinion=False)

        approved, reason = await risk.approve(100.0)
        assert "venue_connectivity" not in reason

    @pytest.mark.asyncio
    async def test_allows_with_opinion_online(self, risk: RiskManager) -> None:
        """Opinion online → allowed (even if Polymarket offline)."""
        await risk.update_venue_status(polymarket=False, opinion=True)

        approved, reason = await risk.approve(100.0)
        assert "venue_connectivity" not in reason


class TestPaperMode:
    """Gate 7: Paper mode approves all trades with 'paper_mode' reason."""

    @pytest.mark.asyncio
    async def test_paper_mode_always_approves(self, paper_risk: RiskManager) -> None:
        """Paper mode should approve even borderline stakes."""
        approved, reason = await paper_risk.approve(100.0)
        assert approved
        assert reason == "paper_mode"

    @pytest.mark.asyncio
    async def test_paper_mode_still_runs_all_checks(self, paper_risk: RiskManager) -> None:
        """Paper mode runs all gates before approving — just doesn't reject."""
        # Simulate kill switch condition in paper mode
        paper_risk._current_bankroll = 0.0

        approved, reason = await paper_risk.approve(100.0)
        # Kill switch fires first, before reaching paper mode check
        assert not approved
        assert "kill_switch" in reason


class TestApprovalHappyPath:
    @pytest.mark.asyncio
    async def test_fresh_manager_approves_small_stake(self, risk: RiskManager) -> None:
        """A fresh RiskManager with no losses should approve small stakes."""
        approved, reason = await risk.approve(100.0)
        assert approved
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_status_snapshot_contains_required_fields(self, risk: RiskManager) -> None:
        """get_status() must return all monitoring fields."""
        status = risk.get_status()
        assert "current_bankroll" in status
        assert "peak_bankroll" in status
        assert "drawdown_pct" in status
        assert "daily_pnl" in status
        assert "consecutive_losses" in status
        assert "cooldown_until" in status
        assert "paper_mode" in status
        assert "kill_switch_active" in status
        assert "venues" in status


class TestDailyReset:
    @pytest.mark.asyncio
    async def test_daily_pnl_resets_at_midnight(self, risk: RiskManager) -> None:
        """Daily P&L should reset when the UTC date changes."""
        import datetime as dt

        risk._daily_pnl = -500.0
        risk._daily_reset_date = (datetime.utcnow() - timedelta(days=1)).date()  # type: ignore

        # Next approval should trigger daily reset
        await risk.approve(100.0)

        assert risk._daily_pnl == 0.0
