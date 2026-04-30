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
import os
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

        # Track whether `sync_bankroll` has run at least once. The first
        # sync rebaselines peak to the live wallet balance to prevent a
        # stale `STARTING_BANKROLL` env value from triggering false-drawdown
        # kills after withdrawals. See `sync_bankroll` for details.
        self._first_live_sync_done: bool = False

        # Daily tracking
        self._day_start_bankroll = starting_bankroll
        self._daily_pnl: float = 0.0
        self._daily_reset_date = datetime.utcnow().date()

        # Cooldown tracking
        self._consecutive_losses: int = 0
        self._cooldown_until: Optional[datetime] = None

        # Kill switch (manual + automatic) with v11 auto-resume
        self._kill_switch_active: bool = False
        self._kill_switch_triggered_at: Optional[datetime] = None
        self._kill_auto_resume_minutes: int = int(
            os.environ.get("KILL_AUTO_RESUME_MINUTES", "0")
        )

        # Venue connectivity
        self._polymarket_connected: bool = False
        self._opinion_connected: bool = False

        self._lock = asyncio.Lock()

    # ─── Trade Approval ───────────────────────────────────────────────────────

    async def approve(
        self, stake_usd: float, strategy: str = "unknown"
    ) -> tuple[bool, str]:
        """Check all risk gates. Returns (approved, reason)."""
        async with self._lock:
            self._maybe_reset_daily()

            # 1. Kill switch (manual or drawdown)
            if self.is_killed:
                return False, f"kill_switch: active (drawdown {self._drawdown_pct:.1%})"

            # 2. Daily loss limit
            # Skip daily loss limit in paper mode — let it run to collect data
            if not self._paper_mode:
                max_daily_loss = self._day_start_bankroll * runtime.daily_loss_limit_pct
                if self._daily_pnl <= -max_daily_loss:
                    return (
                        False,
                        f"daily_loss_limit: down ${abs(self._daily_pnl):.2f} today",
                    )

            # 3. Position limit
            # Allow up to bankroll × bet_fraction × 1.5 (max price multiplier)
            # The stake calculator already applies its own 5% buffer internally
            max_stake = self._current_bankroll * runtime.bet_fraction * 1.5

            # Apply hard max cap (from config)
            hard_max = runtime.max_position_usd
            max_stake = min(max_stake, hard_max)

            if stake_usd > max_stake:
                return False, f"position_limit: ${stake_usd:.2f} > max ${max_stake:.2f}"

            # 4. Exposure limit
            if self._om:
                open_exposure = await self._om.get_open_exposure_usd()
                max_exposure = self._current_bankroll * runtime.max_open_exposure_pct
                if open_exposure + stake_usd > max_exposure:
                    return (
                        False,
                        f"exposure_limit: ${open_exposure + stake_usd:.2f} > ${max_exposure:.2f}",
                    )

            # 5. Cooldown (skip in paper mode)
            if (
                not self._paper_mode
                and self._cooldown_until
                and datetime.utcnow() < self._cooldown_until
            ):
                remaining = (self._cooldown_until - datetime.utcnow()).seconds
                return (
                    False,
                    f"cooldown: {remaining}s remaining after {runtime.consecutive_loss_cooldown} losses",
                )

            # 6. Venue connectivity
            if not self._polymarket_connected and not self._opinion_connected:
                return False, "venue_connectivity: both venues offline"

            log.info(
                "risk.approved",
                strategy=strategy,
                stake=stake_usd,
                paper=self._paper_mode,
            )
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
                    self._cooldown_until = datetime.utcnow() + timedelta(
                        seconds=runtime.cooldown_seconds
                    )
                    log.warning(
                        "risk.cooldown_triggered", losses=self._consecutive_losses
                    )
            else:
                self._consecutive_losses = 0
                # Clear cooldown on win
                self._cooldown_until = None

            # Auto kill switch on drawdown (v11: record timestamp for auto-resume)
            if self._drawdown_pct >= runtime.max_drawdown_kill:
                if not self._kill_switch_active:
                    self._kill_switch_triggered_at = datetime.utcnow()
                self._kill_switch_active = True
                log.critical("risk.drawdown_kill", drawdown=f"{self._drawdown_pct:.1%}")

    # ─── Kill Switch ──────────────────────────────────────────────────────────

    async def sync_bankroll(
        self,
        wallet_balance: float,
        *,
        usdc: Optional[float] = None,
        pusd: Optional[float] = None,
    ) -> None:
        """Sync internal bankroll from the real wallet balance.

        Called periodically from the orchestrator heartbeat. ``wallet_balance``
        should be the *total effective balance* (USDC + pUSD) so that money
        moving between collateral tokens doesn't trigger false drawdown kills.

        In paper mode, wallet_balance will be $0, so we skip sync to preserve
        the paper bankroll tracking. In live mode, we sync from the wallet.

        Withdrawal-aware peak (PR #427)
        ===============================
        On the FIRST live sync after engine boot, we rebaseline the peak
        to the actual wallet balance instead of `max(starting_bankroll,
        wallet_balance)`. This prevents a stale `STARTING_BANKROLL` env
        value (often a high-water mark from a previous session) from
        triggering false drawdown-kill alerts after USDC withdrawals.

        Example failure mode this fixes:
        - Wallet was once $235 (STARTING_BANKROLL=235 set in env)
        - User withdraws $120 to MetaMask → wallet drops to $115
        - Engine restarts: `_peak_bankroll = $235` (from env constructor)
        - sync_bankroll runs: max($235, $115) = $235 → drawdown shows 51%
        - Kill switch fires "drawdown 51% > 45%" — false signal, no real
          trading drawdown happened, just a wallet movement.

        After first sync rebaseline, subsequent syncs use the standard
        ratchet-up max(...) pattern to track genuine trading drawdowns.

        For mid-session withdrawals (without a restart), call
        `record_withdrawal(amount)` to decrement peak explicitly.
        """
        # Skip sync in paper mode - wallet is $0, we track paper bankroll internally
        if self._paper_mode:
            return

        # In live mode, only sync if we have a valid positive balance
        if wallet_balance is None or wallet_balance <= 0:
            return

        old = self._current_bankroll
        self._current_bankroll = wallet_balance

        if not self._first_live_sync_done:
            # First post-boot live sync: rebaseline peak to the actual
            # wallet so a stale STARTING_BANKROLL env doesn't drive a
            # false drawdown kill on a withdrawal-reduced wallet.
            old_peak = self._peak_bankroll
            self._peak_bankroll = wallet_balance
            self._first_live_sync_done = True
            log.info(
                "risk.peak_rebaselined_first_sync",
                env_starting_bankroll=f"${self._starting_bankroll:.2f}",
                old_peak=f"${old_peak:.2f}",
                new_peak=f"${self._peak_bankroll:.2f}",
                wallet=f"${wallet_balance:.2f}",
            )
        else:
            # Subsequent syncs: standard ratchet-up to track genuine drawdowns
            self._peak_bankroll = max(self._peak_bankroll, wallet_balance)

        if abs(old - wallet_balance) > 1.0:
            log.info(
                "risk.bankroll_synced",
                old=f"${old:.2f}",
                new=f"${wallet_balance:.2f}",
                peak=f"${self._peak_bankroll:.2f}",
                usdc=f"${usdc:.2f}" if usdc is not None else None,
                pusd=f"${pusd:.2f}" if pusd is not None else None,
            )

    async def record_withdrawal(self, amount: float) -> None:
        """Decrement peak_bankroll by the withdrawn USDC amount.

        Use after an externally-driven withdrawal (e.g. USDC -> MetaMask)
        to keep `peak_bankroll` honest mid-session. Without this hook the
        peak stays at its pre-withdrawal high and the engine sees a phantom
        drawdown equal to the withdrawal amount (PR #427).

        Floors peak at current_bankroll to avoid negative drawdowns. Caller
        is responsible for actually moving the funds; this only updates
        the in-memory bookkeeping.

        Restart-only callers can rely on the first-sync rebaseline in
        `sync_bankroll` instead — this method is for mid-session use.
        """
        if amount <= 0:
            return
        async with self._lock:
            old_peak = self._peak_bankroll
            self._peak_bankroll = max(
                self._peak_bankroll - amount, self._current_bankroll
            )
            log.warning(
                "risk.withdrawal_recorded",
                amount=f"${amount:.2f}",
                old_peak=f"${old_peak:.2f}",
                new_peak=f"${self._peak_bankroll:.2f}",
                current=f"${self._current_bankroll:.2f}",
            )

    async def rebaseline_live_bankroll(self, wallet_balance: float) -> None:
        """Reset live risk baselines from the current wallet balance.

        Used on a paper -> live mode switch so paper-mode bankroll and peak
        history do not immediately trigger a false live drawdown kill.
        """
        if wallet_balance is None or wallet_balance <= 0:
            return

        self._current_bankroll = wallet_balance
        self._peak_bankroll = wallet_balance
        self._day_start_bankroll = wallet_balance
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._cooldown_until = None
        self._kill_switch_active = False
        self._kill_switch_triggered_at = None
        # Treat explicit rebaseline as the first live sync — prevents the
        # next sync_bankroll call from rebaselining away from the rebase
        # value (PR #427).
        self._first_live_sync_done = True

        log.warning(
            "risk.live_bankroll_rebased",
            bankroll=f"${wallet_balance:.2f}",
        )

    async def force_kill(self, reason: str = "Manual kill") -> None:
        """Manually activate kill switch."""
        if not self._kill_switch_active:
            self._kill_switch_triggered_at = datetime.utcnow()
        self._kill_switch_active = True
        log.warning("risk.force_kill", reason=reason)

    async def resume(self) -> None:
        """Clear manual kill switch. Drawdown kill auto-clears when bankroll recovers."""
        self._kill_switch_active = False
        self._kill_switch_triggered_at = None
        log.info("risk.resumed")

    @property
    def is_killed(self) -> bool:
        """True when trading is halted (manual or drawdown).

        v11: Auto-resume after KILL_AUTO_RESUME_MINUTES cooldown.
        The drawdown check still gates if bankroll hasn't recovered,
        but the manual flag clears so force_kill doesn't persist forever.
        """
        if (
            self._kill_switch_active
            and self._kill_auto_resume_minutes > 0
            and self._kill_switch_triggered_at
            and (datetime.utcnow() - self._kill_switch_triggered_at).total_seconds()
            > self._kill_auto_resume_minutes * 60
        ):
            log.warning(
                "risk.kill_auto_resumed",
                minutes=self._kill_auto_resume_minutes,
                drawdown=f"{self._drawdown_pct:.1%}",
            )
            self._kill_switch_active = False
            self._kill_switch_triggered_at = None

        return (
            self._kill_switch_active or self._drawdown_pct >= runtime.max_drawdown_kill
        )

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
            "cooldown_until": self._cooldown_until.isoformat()
            if self._cooldown_until
            else None,
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

    async def set_paper_bankroll(self, amount: float) -> None:
        """Set the paper trading bankroll explicitly.

        In paper mode, the Polymarket wallet shows $0, so we need to
        track paper bankroll separately. Call this to initialize or
        adjust the paper bankroll (e.g., on engine start or deposit).
        """
        if not self._paper_mode:
            log.warning("risk.set_paper_bankroll_skipped", reason="not in paper mode")
            return

        old = self._current_bankroll
        self._current_bankroll = amount
        self._peak_bankroll = max(self._peak_bankroll, amount)
        self._day_start_bankroll = amount  # Reset daily tracking too

        log.info(
            "risk.paper_bankroll_set",
            old=f"${old:.2f}",
            new=f"${amount:.2f}",
            peak=f"${self._peak_bankroll:.2f}",
        )

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
