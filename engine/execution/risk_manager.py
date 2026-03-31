"""
Risk Manager

Gates ALL trade execution. Checks:
  1. Kill switch (45% drawdown from peak bankroll)
  2. Daily loss limit (10% of starting bankroll for the day)
  3. Position limit (2.5% of bankroll per individual bet)
  4. Exposure limit (30% of bankroll in open positions simultaneously)
  5. Cooldown (3 consecutive losses → 15-minute trading pause)
  6. Venue connectivity (Polymarket + Opinion both reachable)
  7. Paper mode (always approve, but tag as simulated)

Returns (bool, str) — (approved, reason).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional
import structlog

from config.constants import (
    MAX_DRAWDOWN_KILL,
    BET_FRACTION,
    MAX_OPEN_EXPOSURE_PCT,
    DAILY_LOSS_LIMIT_PCT,
    CONSECUTIVE_LOSS_COOLDOWN,
    COOLDOWN_SECONDS,
)
from execution.order_manager import OrderManager

log = structlog.get_logger(__name__)


class RiskManager:
    """
    Central risk gate. Every strategy MUST call `approve(stake_usd)` before
    submitting an order. If this returns (False, reason), the trade must be
    abandoned.

    Thread-safe via asyncio.Lock.
    """

    def __init__(
        self,
        order_manager: OrderManager,
        starting_bankroll: float,
        paper_mode: bool = False,
    ) -> None:
        self._om = order_manager
        self._starting_bankroll = starting_bankroll
        self._peak_bankroll = starting_bankroll
        self._current_bankroll = starting_bankroll
        self._paper_mode = paper_mode

        # Daily tracking (reset at midnight UTC)
        self._day_start_bankroll = starting_bankroll
        self._daily_pnl: float = 0.0
        self._daily_reset_date: datetime = datetime.utcnow().date()  # type: ignore[assignment]

        # Cooldown tracking
        self._consecutive_losses: int = 0
        self._cooldown_until: Optional[datetime] = None

        # Venue connectivity flags (updated externally)
        self._polymarket_connected: bool = True
        self._opinion_connected: bool = True

        self._lock = asyncio.Lock()

    # ─── Public API ───────────────────────────────────────────────────────────

    async def approve(self, stake_usd: float, strategy: str = "unknown") -> tuple[bool, str]:
        """
        Check all risk gates and return (approved, reason).

        Args:
            stake_usd: The proposed bet size in USD.
            strategy:  Name of the calling strategy for logging.

        Returns:
            (True, "paper_mode") if paper_mode is active.
            (True, "ok") if all gates pass.
            (False, "<reason>") if any gate fails.
        """
        async with self._lock:
            self._maybe_reset_daily()

            # 1. Kill switch
            if self._is_kill_switch_triggered():
                return False, f"kill_switch: drawdown exceeds {MAX_DRAWDOWN_KILL:.0%}"

            # 2. Daily loss limit
            if self._is_daily_loss_limit_hit():
                return False, f"daily_loss_limit: down {abs(self._daily_pnl):.2f} today"

            # 3. Position limit
            if stake_usd > self._current_bankroll * BET_FRACTION:
                max_stake = self._current_bankroll * BET_FRACTION
                return False, f"position_limit: stake {stake_usd:.2f} > max {max_stake:.2f}"

            # 4. Exposure limit
            open_exposure = await self._om.get_open_exposure_usd()
            if open_exposure + stake_usd > self._current_bankroll * MAX_OPEN_EXPOSURE_PCT:
                return False, (
                    f"exposure_limit: {open_exposure + stake_usd:.2f} would exceed "
                    f"{self._current_bankroll * MAX_OPEN_EXPOSURE_PCT:.2f}"
                )

            # 5. Cooldown
            if self._is_in_cooldown():
                remaining = (self._cooldown_until - datetime.utcnow()).seconds  # type: ignore[operator]
                return False, f"cooldown: {remaining}s remaining after {CONSECUTIVE_LOSS_COOLDOWN} consecutive losses"

            # 6. Venue connectivity
            venue_ok, venue_reason = self._check_venue_connectivity()
            if not venue_ok:
                return False, venue_reason

            # 7. Paper mode (after all checks so we still log gate states)
            if self._paper_mode:
                log.info("risk.approved_paper", strategy=strategy, stake=stake_usd)
                return True, "paper_mode"

            log.info("risk.approved", strategy=strategy, stake=stake_usd)
            return True, "ok"

    async def record_outcome(self, pnl_usd: float) -> None:
        """
        Record a trade result to update daily P&L and consecutive loss streak.

        Args:
            pnl_usd: Positive for win, negative for loss (net of fees).
        """
        async with self._lock:
            self._daily_pnl += pnl_usd
            self._current_bankroll += pnl_usd
            self._peak_bankroll = max(self._peak_bankroll, self._current_bankroll)

            if pnl_usd < 0:
                self._consecutive_losses += 1
                if self._consecutive_losses >= CONSECUTIVE_LOSS_COOLDOWN:
                    self._cooldown_until = datetime.utcnow() + timedelta(seconds=COOLDOWN_SECONDS)
                    log.warning(
                        "risk.cooldown_triggered",
                        consecutive_losses=self._consecutive_losses,
                        resume_at=self._cooldown_until.isoformat(),
                    )
            else:
                self._consecutive_losses = 0

    async def update_venue_status(self, polymarket: bool, opinion: bool) -> None:
        """Update connectivity flags for Polymarket and Opinion venues."""
        async with self._lock:
            self._polymarket_connected = polymarket
            self._opinion_connected = opinion

    async def set_paper_mode(self, enabled: bool) -> None:
        """Enable or disable paper trading mode at runtime."""
        async with self._lock:
            self._paper_mode = enabled
            log.info("risk.paper_mode_changed", enabled=enabled)

    def get_status(self) -> dict:
        """Return a snapshot of current risk state for monitoring."""
        return {
            "current_bankroll": self._current_bankroll,
            "peak_bankroll": self._peak_bankroll,
            "drawdown_pct": 1 - self._current_bankroll / self._peak_bankroll if self._peak_bankroll else 0,
            "daily_pnl": self._daily_pnl,
            "consecutive_losses": self._consecutive_losses,
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "paper_mode": self._paper_mode,
            "kill_switch_active": self._is_kill_switch_triggered(),
            "venues": {
                "polymarket": self._polymarket_connected,
                "opinion": self._opinion_connected,
            },
        }

    # ─── Internal Checks ──────────────────────────────────────────────────────

    def _is_kill_switch_triggered(self) -> bool:
        """True if drawdown from peak exceeds MAX_DRAWDOWN_KILL (45%)."""
        if self._peak_bankroll <= 0:
            return False
        drawdown = 1 - self._current_bankroll / self._peak_bankroll
        return drawdown >= MAX_DRAWDOWN_KILL

    def _is_daily_loss_limit_hit(self) -> bool:
        """True if daily losses exceed DAILY_LOSS_LIMIT_PCT (10%) of day-start bankroll."""
        max_daily_loss = self._day_start_bankroll * DAILY_LOSS_LIMIT_PCT
        return self._daily_pnl <= -max_daily_loss

    def _is_in_cooldown(self) -> bool:
        """True if currently within a loss-streak cooldown window."""
        if self._cooldown_until is None:
            return False
        if datetime.utcnow() >= self._cooldown_until:
            self._cooldown_until = None
            self._consecutive_losses = 0
            return False
        return True

    def _check_venue_connectivity(self) -> tuple[bool, str]:
        """Verify at least one execution venue is reachable."""
        if not self._polymarket_connected and not self._opinion_connected:
            return False, "venue_connectivity: both Polymarket and Opinion are offline"
        return True, "ok"

    def _maybe_reset_daily(self) -> None:
        """Reset daily loss tracking at midnight UTC."""
        today = datetime.utcnow().date()
        if today != self._daily_reset_date:
            log.info("risk.daily_reset", prev_pnl=self._daily_pnl)
            self._daily_pnl = 0.0
            self._day_start_bankroll = self._current_bankroll
            self._daily_reset_date = today  # type: ignore[assignment]
