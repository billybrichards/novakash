"""
Tests for RiskManager.

Tests all 7 gates: kill switch, daily loss, position limit, exposure limit,
cooldown, venue connectivity, and paper mode.
Tests force_kill + resume and consecutive loss cooldown trigger.

Gate ordering (current production behavior):
  1. kill switch
  2. daily loss limit  (skipped in paper mode)
  3. position limit    (applies in paper mode too — hard_max = MAX_POSITION_USD = $5)
  4. exposure limit    (applies in paper mode too)
  5. cooldown          (skipped in paper mode)
  6. venue connectivity (applies in paper mode too — need at least one venue up)
  7. return ok/paper_mode

Paper mode: only skips daily_loss and cooldown. Other gates still apply.
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from execution.risk_manager import RiskManager
from execution.order_manager import OrderManager


# ─── Fixtures ─────────────────────────────────────────────────────────────────

# In test env: runtime.bet_fraction=0.025, runtime.max_position_usd=5.0
# So max_stake = min(bankroll * 0.025 * 1.5, 5.0) = 5.0 for bankroll >= 133
# max_open_exposure_pct=0.30, so max_exposure = bankroll * 0.30
MAX_STAKE = 5.0  # max_position_usd hard cap from runtime config


def _make_order_manager(open_exposure: float = 0.0) -> MagicMock:
    """Create a mock OrderManager that returns a fixed open exposure."""
    om = MagicMock(spec=OrderManager)
    om.get_open_exposure_usd = AsyncMock(return_value=open_exposure)
    return om


def _make_risk_manager(
    starting_bankroll: float = 1000.0,
    paper_mode: bool = False,
    open_exposure: float = 0.0,
) -> RiskManager:
    """Create a RiskManager with default test settings.

    Note: By default, both venues start disconnected. Tests that expect
    approval must call update_venue_status to bring a venue online first.
    """
    om = _make_order_manager(open_exposure=open_exposure)
    return RiskManager(
        order_manager=om,
        starting_bankroll=starting_bankroll,
        paper_mode=paper_mode,
    )


async def _live_rm_ready(starting_bankroll: float = 1000.0, open_exposure: float = 0.0) -> RiskManager:
    """Live-mode RiskManager with Polymarket online — ready to approve."""
    rm = _make_risk_manager(starting_bankroll=starting_bankroll, open_exposure=open_exposure)
    await rm.update_venue_status(polymarket=True, opinion=False)
    return rm


async def _paper_rm_ready(starting_bankroll: float = 1000.0, open_exposure: float = 0.0) -> RiskManager:
    """Paper-mode RiskManager with venue online — ready to approve."""
    rm = _make_risk_manager(starting_bankroll=starting_bankroll, paper_mode=True, open_exposure=open_exposure)
    await rm.update_venue_status(polymarket=True, opinion=False)
    return rm


# ─── Gate 7: Paper Mode ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paper_mode_always_approves():
    """In paper mode with venue online and small stake, reason is 'paper_mode'."""
    rm = await _paper_rm_ready()

    approved, reason = await rm.approve(MAX_STAKE)

    assert approved is True
    assert reason == "paper_mode"


@pytest.mark.asyncio
async def test_paper_mode_approves_despite_high_stake():
    """Paper mode still applies position_limit gate — stake must be within hard cap.

    With starting_bankroll=100: max_stake = min(100*0.025*1.5, 5.0) = min(3.75, 5.0) = 3.75.
    Use a stake at the bankroll-derived cap, not the global hard cap.
    """
    rm = await _paper_rm_ready(starting_bankroll=100.0)

    # 3.0 < 3.75 (bankroll-derived cap) — paper mode approves
    approved, reason = await rm.approve(3.0)

    assert approved is True
    assert reason == "paper_mode"


# ─── Gate 1: Kill Switch ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kill_switch_blocks_after_45pct_drawdown():
    """Kill switch activates at >= 45% drawdown from peak.

    Lose $451 (45.1%) to ensure we cross the 0.45 floating-point threshold.
    Paper mode used so daily_loss_limit doesn't fire first (it only blocks live trades).
    """
    rm = await _paper_rm_ready(starting_bankroll=1000.0)

    # 45.1% drawdown — crosses threshold even with floating-point imprecision
    await rm.record_outcome(-451.0)

    # Kill switch gate fires before any other
    approved, reason = await rm.approve(1.0)

    assert approved is False
    assert "kill_switch" in reason


@pytest.mark.asyncio
async def test_kill_switch_requires_manual_resume():
    """Kill switch stays active even on subsequent calls until resume()."""
    rm = _make_risk_manager(starting_bankroll=1000.0)

    # Force kill manually (async in production)
    await rm.force_kill()

    approved, reason = await rm.approve(1.0)
    assert approved is False
    assert "kill_switch" in reason

    # Still blocked
    approved2, _ = await rm.approve(MAX_STAKE)
    assert approved2 is False


@pytest.mark.asyncio
async def test_force_kill_and_resume():
    """force_kill blocks; resume() restores normal operation."""
    rm = await _live_rm_ready(starting_bankroll=1000.0)

    await rm.force_kill()
    assert rm.is_killed is True

    blocked, _ = await rm.approve(1.0)
    assert blocked is False

    await rm.resume()
    assert rm.is_killed is False

    # Now should be able to approve small stakes
    approved, reason = await rm.approve(1.0)
    assert approved is True
    assert reason == "ok"


@pytest.mark.asyncio
async def test_kill_switch_not_triggered_below_threshold():
    """Kill switch should not trigger at < 45% drawdown.

    Paper mode is used so daily_loss_limit (which fires at 10%) does not
    block the test before we can verify the kill-switch gate.
    """
    rm = await _paper_rm_ready(starting_bankroll=1000.0)

    # Lose 40% — below 45% threshold
    await rm.record_outcome(-400.0)

    # Kill switch does NOT fire; paper mode skips daily_loss gate; approves
    approved, reason = await rm.approve(1.0)
    assert approved is True
    assert reason == "paper_mode"


# ─── Gate 2: Daily Loss Limit ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_loss_limit_blocks_at_10pct():
    """Daily loss limit activates at >= 10% of day-start bankroll."""
    rm = await _live_rm_ready(starting_bankroll=1000.0)

    # Lose exactly 10% of 1000 = 100
    await rm.record_outcome(-100.0)

    approved, reason = await rm.approve(1.0)
    assert approved is False
    assert "daily_loss_limit" in reason


@pytest.mark.asyncio
async def test_daily_loss_does_not_block_below_threshold():
    """Daily loss below 10% should not block trading."""
    rm = await _live_rm_ready(starting_bankroll=1000.0)

    # Lose 5% — below threshold
    await rm.record_outcome(-50.0)

    approved, reason = await rm.approve(1.0)
    assert approved is True


# ─── Gate 3: Position Limit ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_position_limit_blocks_large_stake():
    """Position limit blocks stake > max_position_usd hard cap ($5)."""
    rm = await _live_rm_ready(starting_bankroll=1000.0)

    # Exceeds hard_max=$5
    approved, reason = await rm.approve(6.0)

    assert approved is False
    assert "position_limit" in reason


@pytest.mark.asyncio
async def test_position_limit_allows_correct_stake():
    """Position limit allows stake at the hard cap limit."""
    rm = await _live_rm_ready(starting_bankroll=1000.0)

    approved, reason = await rm.approve(MAX_STAKE)
    assert approved is True


# ─── Gate 4: Exposure Limit ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exposure_limit_blocks_when_too_much_open():
    """Exposure limit blocks when adding stake would exceed 30% of bankroll."""
    # Already have $298 open exposure (29.8%) with 1000 bankroll
    # Adding $5 would bring to $303, exceeding 30% = $300
    rm = await _live_rm_ready(starting_bankroll=1000.0, open_exposure=298.0)

    approved, reason = await rm.approve(MAX_STAKE)
    assert approved is False
    assert "exposure_limit" in reason


@pytest.mark.asyncio
async def test_exposure_limit_allows_when_under():
    """Exposure limit allows trade when total stays below 30%."""
    # $200 open, adding $5 = $205 < 30% of $1000 = $300
    rm = await _live_rm_ready(starting_bankroll=1000.0, open_exposure=200.0)

    approved, reason = await rm.approve(MAX_STAKE)
    assert approved is True


# ─── Gate 5: Cooldown ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_triggers_after_consecutive_losses():
    """Cooldown activates after CONSECUTIVE_LOSS_COOLDOWN (3) consecutive losses."""
    rm = await _live_rm_ready(starting_bankroll=1000.0)

    # Record 3 consecutive losses
    await rm.record_outcome(-5.0)
    await rm.record_outcome(-5.0)
    await rm.record_outcome(-5.0)

    approved, reason = await rm.approve(1.0)
    assert approved is False
    assert "cooldown" in reason


@pytest.mark.asyncio
async def test_win_resets_consecutive_losses():
    """A win between losses should reset the consecutive loss counter."""
    rm = await _live_rm_ready(starting_bankroll=1000.0)

    await rm.record_outcome(-5.0)  # loss 1
    await rm.record_outcome(-5.0)  # loss 2
    await rm.record_outcome(10.0)  # WIN — resets counter
    await rm.record_outcome(-5.0)  # loss 1 again (counter reset)

    # Only 1 consecutive loss after the win — should not be in cooldown
    approved, reason = await rm.approve(1.0)
    assert approved is True


@pytest.mark.asyncio
async def test_cooldown_expires():
    """Cooldown should expire after COOLDOWN_SECONDS."""
    rm = await _live_rm_ready(starting_bankroll=1000.0)

    await rm.record_outcome(-5.0)
    await rm.record_outcome(-5.0)
    await rm.record_outcome(-5.0)

    # Manually set cooldown expiry to the past
    rm._cooldown_until = datetime.utcnow() - timedelta(seconds=1)

    approved, reason = await rm.approve(1.0)
    assert approved is True


# ─── Gate 6: Venue Connectivity ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_venue_connectivity_blocks_when_both_offline():
    """Trading is blocked when both Polymarket and Opinion are offline."""
    rm = _make_risk_manager(starting_bankroll=1000.0)
    # Both offline (default state)

    approved, reason = await rm.approve(1.0)
    assert approved is False
    assert "venue_connectivity" in reason


@pytest.mark.asyncio
async def test_venue_connectivity_allows_when_polymarket_online():
    """Trading proceeds when at least Polymarket is online."""
    rm = _make_risk_manager(starting_bankroll=1000.0)
    await rm.update_venue_status(polymarket=True, opinion=False)

    approved, reason = await rm.approve(1.0)
    assert approved is True


@pytest.mark.asyncio
async def test_venue_connectivity_allows_when_opinion_online():
    """Trading proceeds when at least Opinion is online."""
    rm = _make_risk_manager(starting_bankroll=1000.0)
    await rm.update_venue_status(polymarket=False, opinion=True)

    approved, reason = await rm.approve(1.0)
    assert approved is True


# ─── get_status ───────────────────────────────────────────────────────────────

def test_get_status_returns_expected_fields():
    """get_status should return all expected risk state fields."""
    rm = _make_risk_manager(starting_bankroll=1000.0)
    status = rm.get_status()

    required_keys = {
        "current_bankroll",
        "peak_bankroll",
        "drawdown_pct",
        "daily_pnl",
        "consecutive_losses",
        "cooldown_until",
        "paper_mode",
        "kill_switch_active",
        "venues",
    }
    assert required_keys.issubset(status.keys())


def test_get_status_initial_state():
    """Initial status should reflect starting bankroll with no losses."""
    rm = _make_risk_manager(starting_bankroll=500.0)
    status = rm.get_status()

    assert status["current_bankroll"] == 500.0
    assert status["peak_bankroll"] == 500.0
    assert status["drawdown_pct"] == 0.0
    assert status["daily_pnl"] == 0.0
    assert status["consecutive_losses"] == 0
    assert status["cooldown_until"] is None
    assert status["kill_switch_active"] is False


# ─── record_outcome ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_outcome_updates_bankroll():
    """record_outcome should update current_bankroll."""
    rm = _make_risk_manager(starting_bankroll=1000.0)

    await rm.record_outcome(50.0)   # win
    status = rm.get_status()
    assert status["current_bankroll"] == 1050.0

    await rm.record_outcome(-30.0)  # loss
    status = rm.get_status()
    assert status["current_bankroll"] == 1020.0


@pytest.mark.asyncio
async def test_record_outcome_updates_peak_bankroll():
    """Peak bankroll should update on wins but not decrease on losses."""
    rm = _make_risk_manager(starting_bankroll=1000.0)

    await rm.record_outcome(100.0)
    assert rm.get_status()["peak_bankroll"] == 1100.0

    await rm.record_outcome(-200.0)
    assert rm.get_status()["peak_bankroll"] == 1100.0  # Peak unchanged


# ─── Multiple Gates Integration ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kill_switch_checked_before_other_gates():
    """Kill switch should be checked before venue connectivity and other gates."""
    # Even with tiny stake, kill switch blocks before venue gate
    rm = _make_risk_manager(starting_bankroll=1000.0)
    await rm.force_kill()

    approved, reason = await rm.approve(0.01)  # Tiny stake
    assert approved is False
    assert "kill_switch" in reason
