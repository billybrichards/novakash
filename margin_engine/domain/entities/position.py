"""
Position entity — lifecycle from entry to close.

A Position tracks a single leveraged trade on Binance cross-margin.
State transitions: PENDING_ENTRY → OPEN → PENDING_EXIT → CLOSED.
The entity enforces invariants: you can't close an unopened position,
you can't open a position twice, P&L is only computed on CLOSED state.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from margin_engine.domain.value_objects import (
    ExitReason,
    Money,
    PositionState,
    Price,
    StopLevel,
    TradeSide,
)


@dataclass
class Position:
    """
    Mutable aggregate for a single margin trade.

    Created in PENDING_ENTRY state. Transitions:
      confirm_entry() → OPEN
      request_exit()  → PENDING_EXIT
      confirm_exit()  → CLOSED
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    asset: str = "BTC"
    side: TradeSide = TradeSide.LONG
    state: PositionState = PositionState.PENDING_ENTRY
    leverage: int = 5

    # Entry
    entry_price: Optional[Price] = None
    notional: Optional[Money] = None  # position size (with leverage)
    collateral: Optional[Money] = None  # actual capital committed

    # Risk levels
    stop_loss: Optional[StopLevel] = None
    take_profit: Optional[StopLevel] = None
    trailing_stop: Optional[StopLevel] = None

    # Exit
    exit_price: Optional[Price] = None
    exit_reason: Optional[ExitReason] = None
    realised_pnl: float = 0.0  # signed, in USDT

    # Timing
    opened_at: float = 0.0
    closed_at: float = 0.0
    max_hold_seconds: int = 3600  # default 1 hour max hold

    # Signal context
    entry_signal_score: float = 0.0
    entry_timescale: str = "5m"

    # Binance order IDs
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None

    # ─── State transitions ───────────────────────────────────────────────

    def confirm_entry(self, price: Price, notional: Money, collateral: Money, order_id: str) -> None:
        """Transition PENDING_ENTRY → OPEN."""
        if self.state != PositionState.PENDING_ENTRY:
            raise ValueError(f"Cannot confirm entry in state {self.state}")
        self.entry_price = price
        self.notional = notional
        self.collateral = collateral
        self.entry_order_id = order_id
        self.state = PositionState.OPEN
        self.opened_at = time.time()

    def request_exit(self, reason: ExitReason) -> None:
        """Transition OPEN → PENDING_EXIT."""
        if self.state != PositionState.OPEN:
            raise ValueError(f"Cannot request exit in state {self.state}")
        self.exit_reason = reason
        self.state = PositionState.PENDING_EXIT

    def confirm_exit(self, price: Price, order_id: str) -> None:
        """Transition PENDING_EXIT → CLOSED, compute P&L."""
        if self.state != PositionState.PENDING_EXIT:
            raise ValueError(f"Cannot confirm exit in state {self.state}")
        self.exit_price = price
        self.exit_order_id = order_id
        self.state = PositionState.CLOSED
        self.closed_at = time.time()
        self.realised_pnl = self._compute_pnl()

    # ─── P&L ─────────────────────────────────────────────────────────────

    def _compute_pnl(self) -> float:
        """Compute realised P&L including estimated fees."""
        if not self.entry_price or not self.exit_price or not self.notional:
            return 0.0

        entry = self.entry_price.value
        exit_ = self.exit_price.value
        notional_usd = self.notional.amount

        if self.side == TradeSide.LONG:
            raw_pnl = (exit_ - entry) / entry * notional_usd
        else:
            raw_pnl = (entry - exit_) / entry * notional_usd

        # Fees: 0.075% per side with BNB discount = 0.15% round-trip
        fee_rate = 0.00075  # per side
        total_fees = notional_usd * fee_rate * 2

        # Borrow interest: ~0.008%/day, pro-rated
        hold_seconds = self.closed_at - self.opened_at
        daily_borrow_rate = 0.00008
        borrow_cost = notional_usd * daily_borrow_rate * (hold_seconds / 86400)

        return raw_pnl - total_fees - borrow_cost

    def unrealised_pnl(self, current_price: float) -> float:
        """Compute unrealised P&L at a given price."""
        if self.state != PositionState.OPEN or not self.entry_price or not self.notional:
            return 0.0

        entry = self.entry_price.value
        notional_usd = self.notional.amount

        if self.side == TradeSide.LONG:
            return (current_price - entry) / entry * notional_usd
        else:
            return (entry - current_price) / entry * notional_usd

    # ─── Risk checks ─────────────────────────────────────────────────────

    def should_stop_loss(self, current_price: float) -> bool:
        """Check if current price has hit the stop-loss level."""
        if not self.stop_loss:
            return False
        if self.side == TradeSide.LONG:
            return current_price <= self.stop_loss.price
        else:
            return current_price >= self.stop_loss.price

    def should_take_profit(self, current_price: float) -> bool:
        """Check if current price has hit the take-profit level."""
        if not self.take_profit:
            return False
        if self.side == TradeSide.LONG:
            return current_price >= self.take_profit.price
        else:
            return current_price <= self.take_profit.price

    def is_expired(self) -> bool:
        """Check if position has exceeded max hold time."""
        if self.state != PositionState.OPEN:
            return False
        return (time.time() - self.opened_at) > self.max_hold_seconds

    @property
    def hold_duration_s(self) -> float:
        if self.state == PositionState.CLOSED:
            return self.closed_at - self.opened_at
        elif self.state == PositionState.OPEN:
            return time.time() - self.opened_at
        return 0.0
