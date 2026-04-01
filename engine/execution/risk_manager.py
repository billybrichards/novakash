"""
RiskManager — Gates ALL trade execution.

Checks (in order):
1. Kill switch: drawdown from peak > 45% OR manual kill active
2. Daily loss limit: today's losses > 10% of starting balance
3. Position limit: proposed stake > 2.5% of current balance
4. Exposure limit: total open positions > 30% of balance
5. Cooldown: 3 consecutive losses → 15 min pause
6. Venue connectivity: at least one venue reachable
7. Paper mode: always approve but tag as paper

Returns (bool, str) — (approved, reason).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Optional

import structlog

from config.runtime_config import runtime

log = structlog.get_logger(__name__)


class RiskManager:
    """
    Central risk gate. Every strategy MUST call approve(stake_usd) before
    submitting an order.
    """

    def __init__(
        self,
        order_manager=None,
        starting_bankroll: float = 500.0,
        paper_mode: bool = True,
    ) -> None:
        self._om = order_manager
        self._starting_bankroll = starting_bankroll
        self._current_bankroll = starting_bankroll
        self._peak_bankroll = starting_bankroll
        self._paper_mode = paper_mode

        # Daily tracking
        self._day_start_bankroll = starting_bankroll
        self._daily_pnl: float = 0.0
        self._daily_reset_date = datetime.utcnow().date()

        # Cooldown tracking
        self._consecutive_losses: int = 0
        self._cooldown_until: Optional[datetime] = None

        # Kill switch (manual + automatic)
        self._kill_switch_active: bool = False

        # Venue connectivity
        self._polymarket_connected: bool = False
        self._opinion_connected: bool = False

        self._lock = asyncio.Lock()

    # ─── Trade Approval ───────────────────────────────────────────────────────

    async def approve(self, stake_usd: float, strategy: str = "unknown") -> tuple[bool, str]:
        """Check all risk gates. Returns (approved, reason)."""
        async with self._lock:
            self._maybe_reset_daily()

            # 1. Kill switch (manual or drawdown)
            if self.is_killed:
                return False, f"kill_switch: active (drawdown {self._drawdown_pct:.1%})"

            # 2. Daily loss limit
            max_daily_loss = self._day_start_bankroll * runtime.daily_loss_limit_pct
            if self._daily_pnl <= -max_daily_loss:
                return False, f"daily_loss_limit: down ${abs(self._daily_pnl):.2f} today"

            # 3. Position limit
            max_stake = self._current_bankroll * runtime.bet_fraction
            if stake_usd > max_stake:
                return False, f"position_limit: ${stake_usd:.2f} > max ${max_stake:.2f}"

            # 4. Exposure limit
            if self._om:
                open_exposure = await self._om.get_open_exposure_usd()
                max_exposure = self._current_bankroll * runtime.max_open_exposure_pct
                if open_exposure + stake_usd > max_exposure:
                    return False, f"exposure_limit: ${open_exposure + stake_usd:.2f} > ${max_exposure:.2f}"

            # 5. Cooldown
            if self._cooldown_until and datetime.utcnow() < self._cooldown_until:
                remaining = (self._cooldown_until - datetime.utcnow()).seconds
                return False, f"cooldown: {remaining}s remaining after {runtime.consecutive_loss_cooldown} losses"

            # 6. Venue connectivity
            if not self._polymarket_connected and not self._opinion_connected:
                return False, "venue_connectivity: both venues offline"

            log.info("risk.approved", strategy=strategy, stake=stake_usd, paper=self._paper_mode)
            return True, "paper_mode" if self._paper_mode else "ok"

    # ─── Outcome Recording ────────────────────────────────────────────────────

    async def record_outcome(self, pnl_usd: float) -> None:
        """Record a trade result to update bankroll and streak tracking."""
        async with self._lock:
            self._daily_pnl += pnl_usd
            self._current_bankroll += pnl_usd
            self._peak_bankroll = max(self._peak_bankroll, self._current_bankroll)

            if pnl_usd < 0:
                self._consecutive_losses += 1
                if self._consecutive_losses >= runtime.consecutive_loss_cooldown:
                    self._cooldown_until = datetime.utcnow() + timedelta(seconds=runtime.cooldown_seconds)
                    log.warning("risk.cooldown_triggered", losses=self._consecutive_losses)
            else:
                self._consecutive_losses = 0
                # Clear cooldown on win
                self._cooldown_until = None

            # Auto kill switch on drawdown
            if self._drawdown_pct >= runtime.max_drawdown_kill:
                self._kill_switch_active = True
                log.critical("risk.drawdown_kill", drawdown=f"{self._drawdown_pct:.1%}")

    # ─── Kill Switch ──────────────────────────────────────────────────────────

    async def force_kill(self, reason: str = "Manual kill") -> None:
        """Manually activate kill switch."""
        self._kill_switch_active = True
        log.warning("risk.force_kill", reason=reason)

    async def resume(self) -> None:
        """Clear manual kill switch. Drawdown kill auto-clears when bankroll recovers."""
        self._kill_switch_active = False
        log.info("risk.resumed")

    @property
    def is_killed(self) -> bool:
        """True when trading is halted (manual or drawdown)."""
        return self._kill_switch_active or self._drawdown_pct >= runtime.max_drawdown_kill

    # ─── Venue Status ─────────────────────────────────────────────────────────

    async def update_venue_status(self, polymarket: bool, opinion: bool) -> None:
        """Update venue connectivity flags."""
        self._polymarket_connected = polymarket
        self._opinion_connected = opinion

    # ─── Status Snapshot ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current risk state for monitoring/dashboard."""
        return {
            "current_bankroll": self._current_bankroll,
            "peak_bankroll": self._peak_bankroll,
            "drawdown_pct": self._drawdown_pct,
            "daily_pnl": self._daily_pnl,
            "consecutive_losses": self._consecutive_losses,
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "paper_mode": self._paper_mode,
            "kill_switch_active": self._kill_switch_active,
            "is_killed": self.is_killed,
            "venues": {
                "polymarket": self._polymarket_connected,
                "opinion": self._opinion_connected,
            },
        }

    # ─── Paper Mode ───────────────────────────────────────────────────────────

    async def set_paper_mode(self, enabled: bool) -> None:
        """Toggle paper trading mode at runtime."""
        self._paper_mode = enabled
        log.info("risk.paper_mode", enabled=enabled)

    # ─── Internal ─────────────────────────────────────────────────────────────

    @property
    def _drawdown_pct(self) -> float:
        """Current drawdown from peak as a fraction (0-1)."""
        if self._peak_bankroll <= 0:
            return 0.0
        return max(0.0, 1 - self._current_bankroll / self._peak_bankroll)

    def _maybe_reset_daily(self) -> None:
        """Reset daily tracking at midnight UTC."""
        today = datetime.utcnow().date()
        if today != self._daily_reset_date:
            log.info("risk.daily_reset", prev_pnl=self._daily_pnl)
            self._daily_pnl = 0.0
            self._day_start_bankroll = self._current_bankroll
            self._daily_reset_date = today
