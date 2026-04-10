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

    # Exchange ground truth (populated from FillResult on confirm_entry/confirm_exit)
    # When commission_is_actual is True, _compute_pnl uses these instead of the
    # fallback 0.1% fee estimate — this is the difference between "we know what
    # we paid" and "we're guessing". Stays 0.0 in legacy code paths (DB restore).
    entry_commission: float = 0.0       # USDT-equivalent, paid at entry
    exit_commission: float = 0.0        # USDT-equivalent, paid at exit
    entry_commission_is_actual: bool = False
    exit_commission_is_actual: bool = False

    # ── Execution context ─────────────────────────────────────────────────
    # Venue this trade was executed on. Informational — the position itself
    # doesn't route orders, but persisting it lets the dashboard attribute
    # historical P&L to the right venue. NULL in legacy rows → "binance".
    venue: str = "binance"
    # Strategy that opened the position. "v1-composite" = pre-PR#8 composite
    # sign entries, "v2-probability" = ML-directed entries via ProbabilitySignal.
    # NULL in legacy rows → "v1-composite" at the DB read layer.
    strategy_version: str = "v2-probability"

    # ─── State transitions ───────────────────────────────────────────────

    def confirm_entry(
        self,
        price: Price,
        notional: Money,
        collateral: Money,
        order_id: str,
        commission: float = 0.0,
        commission_is_actual: bool = False,
    ) -> None:
        """Transition PENDING_ENTRY → OPEN."""
        if self.state != PositionState.PENDING_ENTRY:
            raise ValueError(f"Cannot confirm entry in state {self.state}")
        self.entry_price = price
        self.notional = notional
        self.collateral = collateral
        self.entry_order_id = order_id
        self.entry_commission = commission
        self.entry_commission_is_actual = commission_is_actual
        self.state = PositionState.OPEN
        self.opened_at = time.time()

    def request_exit(self, reason: ExitReason) -> None:
        """Transition OPEN → PENDING_EXIT."""
        if self.state != PositionState.OPEN:
            raise ValueError(f"Cannot request exit in state {self.state}")
        self.exit_reason = reason
        self.state = PositionState.PENDING_EXIT

    def confirm_exit(
        self,
        price: Price,
        order_id: str,
        commission: float = 0.0,
        commission_is_actual: bool = False,
    ) -> None:
        """Transition PENDING_EXIT → CLOSED, compute P&L."""
        if self.state != PositionState.PENDING_EXIT:
            raise ValueError(f"Cannot confirm exit in state {self.state}")
        self.exit_price = price
        self.exit_order_id = order_id
        self.exit_commission = commission
        self.exit_commission_is_actual = commission_is_actual
        self.state = PositionState.CLOSED
        self.closed_at = time.time()
        self.realised_pnl = self._compute_pnl()

    # ─── P&L ─────────────────────────────────────────────────────────────

    # Fallback fee rate when exchange ground truth is unavailable.
    # 0.1% per side = 0.2% round-trip. Conservative (assumes no BNB discount).
    _FALLBACK_FEE_RATE_PER_SIDE: float = 0.001
    # Rough cross-margin borrow rate on USDT at 5x leverage.
    # Real rate varies by tier / hour — update via /sapi/v1/margin/interestRateHistory
    # when we have the appetite to query it per position. For now this is an
    # acknowledged estimate that stays visible in code rather than buried.
    _ESTIMATED_DAILY_BORROW_RATE: float = 0.00008

    def _compute_pnl(self) -> float:
        """
        Compute realised P&L using exchange ground truth when available.

        If both entry and exit commissions came from the exchange
        (commission_is_actual=True), those values feed the calculation directly.
        Otherwise we fall back to a conservative 0.1%-per-side estimate.

        Borrow interest is always estimated — Binance cross-margin doesn't
        report per-position interest, so we can't do better than a rate-based
        approximation of the hold window.
        """
        if not self.entry_price or not self.exit_price or not self.notional:
            return 0.0

        entry = self.entry_price.value
        exit_ = self.exit_price.value
        notional_usd = self.notional.amount

        if self.side == TradeSide.LONG:
            raw_pnl = (exit_ - entry) / entry * notional_usd
        else:
            raw_pnl = (entry - exit_) / entry * notional_usd

        # Prefer actual commissions from the exchange; fall back to estimate
        if self.entry_commission_is_actual and self.exit_commission_is_actual:
            total_fees = self.entry_commission + self.exit_commission
        else:
            total_fees = notional_usd * self._FALLBACK_FEE_RATE_PER_SIDE * 2

        # Borrow interest (estimated — see class comment)
        hold_seconds = max(0.0, self.closed_at - self.opened_at)
        borrow_cost = notional_usd * self._ESTIMATED_DAILY_BORROW_RATE * (hold_seconds / 86400)

        return raw_pnl - total_fees - borrow_cost

    def unrealised_pnl(self, current_price: float) -> float:
        """Raw unrealised P&L at a given price — NO fees, NO borrow interest.

        This is the gross move. For stop-loss / take-profit decisions you
        almost always want unrealised_pnl_net() instead.
        """
        if self.state != PositionState.OPEN or not self.entry_price or not self.notional:
            return 0.0

        entry = self.entry_price.value
        notional_usd = self.notional.amount

        if self.side == TradeSide.LONG:
            return (current_price - entry) / entry * notional_usd
        else:
            return (entry - current_price) / entry * notional_usd

    def unrealised_pnl_net(self, mark_price: float) -> float:
        """
        Net unrealised P&L if we closed right now at `mark_price`, inclusive of:
          - entry commission (already paid)
          - estimated exit commission at the fallback fee rate
          - accrued borrow interest so far

        This is the honest "what's my position actually worth?" number.
        Pass it the mark from ExchangePort.get_mark() (bid for LONG, ask for SHORT)
        to get a number that matches what closing would actually realise.
        """
        if self.state != PositionState.OPEN or not self.entry_price or not self.notional:
            return 0.0

        raw = self.unrealised_pnl(mark_price)
        notional_usd = self.notional.amount

        # Entry fee: real if we have it, otherwise estimate
        if self.entry_commission_is_actual:
            entry_fee = self.entry_commission
        else:
            entry_fee = notional_usd * self._FALLBACK_FEE_RATE_PER_SIDE

        # Exit fee: always estimated at close time, we haven't placed the order yet
        exit_fee_est = notional_usd * self._FALLBACK_FEE_RATE_PER_SIDE

        # Borrow interest accrued so far
        hold_seconds = max(0.0, time.time() - self.opened_at)
        borrow_so_far = notional_usd * self._ESTIMATED_DAILY_BORROW_RATE * (hold_seconds / 86400)

        return raw - entry_fee - exit_fee_est - borrow_so_far

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
