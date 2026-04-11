"""
Portfolio aggregate root — manages open positions and enforces risk gates.

The Portfolio is the single entry point for all position lifecycle operations.
It enforces:
  - Max open positions limit
  - Max total exposure limit
  - Daily loss limit (kill switch)
  - Cooldown after consecutive losses
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from margin_engine.domain.entities.position import Position
from margin_engine.domain.value_objects import (
    ExitReason,
    Money,
    PositionState,
    TradeSide,
)

logger = logging.getLogger(__name__)


@dataclass
class Portfolio:
    """
    Aggregate root for margin trading positions.

    All position operations go through Portfolio to enforce risk invariants.
    """
    starting_capital: Money = field(default_factory=lambda: Money.usd(500.0))
    leverage: int = 5
    max_open_positions: int = 3
    max_exposure_pct: float = 0.60  # max 60% of capital in open positions
    daily_loss_limit_pct: float = 0.10  # 10% daily loss → kill switch
    consecutive_loss_cooldown: int = 3
    cooldown_seconds: int = 600

    # State
    positions: list[Position] = field(default_factory=list)
    _kill_switch: bool = False
    _cooldown_until: float = 0.0
    _consecutive_losses: int = 0
    _daily_pnl: float = 0.0
    _daily_pnl_reset_date: str = ""

    # ─── Risk gates ──────────────────────────────────────────────────────

    def can_open_position(self, collateral: Money) -> tuple[bool, str]:
        """Check if a new position can be opened. Returns (allowed, reason)."""
        if self._kill_switch:
            return False, "kill switch active"

        if time.time() < self._cooldown_until:
            remaining = int(self._cooldown_until - time.time())
            return False, f"cooldown active ({remaining}s remaining)"

        open_positions = [p for p in self.positions if p.state == PositionState.OPEN]
        if len(open_positions) >= self.max_open_positions:
            return False, f"max open positions reached ({self.max_open_positions})"

        current_exposure = sum(
            p.collateral.amount for p in open_positions if p.collateral
        )
        new_exposure = current_exposure + collateral.amount
        max_exposure = self.starting_capital.amount * self.max_exposure_pct
        if new_exposure > max_exposure:
            return False, f"exposure limit: {new_exposure:.2f} > {max_exposure:.2f}"

        # Check daily loss limit
        self._maybe_reset_daily_pnl()
        if abs(self._daily_pnl) > self.starting_capital.amount * self.daily_loss_limit_pct:
            return False, f"daily loss limit reached: {self._daily_pnl:.2f}"

        return True, "ok"

    def activate_kill_switch(self) -> None:
        self._kill_switch = True
        logger.warning("KILL SWITCH ACTIVATED")

    def resume(self) -> None:
        self._kill_switch = False
        self._cooldown_until = 0.0
        self._consecutive_losses = 0
        logger.info("Portfolio resumed from kill switch / cooldown")

    # ─── Position lifecycle ──────────────────────────────────────────────

    def add_position(self, position: Position) -> None:
        """Register a new position (in PENDING_ENTRY state)."""
        self.positions.append(position)

    def on_position_closed(self, position: Position) -> None:
        """Called when a position reaches CLOSED state. Updates risk tracking."""
        self._maybe_reset_daily_pnl()
        self._daily_pnl += position.realised_pnl

        if position.realised_pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.consecutive_loss_cooldown:
                self._cooldown_until = time.time() + self.cooldown_seconds
                logger.warning(
                    "Cooldown triggered: %d consecutive losses, pausing %ds",
                    self._consecutive_losses, self.cooldown_seconds,
                )
                self._consecutive_losses = 0
        else:
            self._consecutive_losses = 0

        # Daily loss kill switch
        if self._daily_pnl < -(self.starting_capital.amount * self.daily_loss_limit_pct):
            self.activate_kill_switch()

    # ─── Queries ─────────────────────────────────────────────────────────

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.state == PositionState.OPEN]

    @property
    def total_exposure(self) -> float:
        return sum(p.collateral.amount for p in self.open_positions if p.collateral)

    @property
    def total_unrealised_pnl(self) -> float:
        # Caller needs to provide current price — this is a convenience placeholder
        return 0.0

    @property
    def total_realised_pnl(self) -> float:
        return sum(
            p.realised_pnl for p in self.positions if p.state == PositionState.CLOSED
        )

    @property
    def win_rate(self) -> float:
        closed = [p for p in self.positions if p.state == PositionState.CLOSED]
        if not closed:
            return 0.0
        wins = sum(1 for p in closed if p.realised_pnl > 0)
        return wins / len(closed)

    @property
    def is_active(self) -> bool:
        return not self._kill_switch and time.time() >= self._cooldown_until

    # ─── Internal ────────────────────────────────────────────────────────

    def _maybe_reset_daily_pnl(self) -> None:
        """Reset daily P&L at midnight UTC."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_pnl_reset_date:
            self._daily_pnl = 0.0
            self._daily_pnl_reset_date = today
