"""
Use case: Manage open positions — check stops, trailing, expiry, signal reversals.

Runs on a loop (typically every 1-2 seconds). For each open position, checks:
  1. Stop-loss hit → close
  2. Take-profit hit → close
  3. Trailing stop update → adjust stop level
  4. Max hold time expired → close
  5. Signal reversal → close

This is the risk management hot path.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from margin_engine.domain.entities.portfolio import Portfolio
from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import (
    AlertPort,
    ExchangePort,
    PositionRepository,
    SignalPort,
)
from margin_engine.domain.value_objects import (
    CompositeSignal,
    ExitReason,
    Money,
    PositionState,
    Price,
    StopLevel,
    TradeSide,
)

logger = logging.getLogger(__name__)


class ManagePositionsUseCase:
    """
    Monitors open positions and closes them when exit conditions are met.

    Called from the main loop every tick. Evaluates all open positions
    against current price and signals.
    """

    def __init__(
        self,
        exchange: ExchangePort,
        portfolio: Portfolio,
        repository: PositionRepository,
        alerts: AlertPort,
        signal_port: SignalPort,
        trailing_stop_pct: float = 0.01,
        signal_reversal_threshold: float = -0.2,
    ) -> None:
        self._exchange = exchange
        self._portfolio = portfolio
        self._repo = repository
        self._alerts = alerts
        self._signal_port = signal_port
        self._trailing_pct = trailing_stop_pct
        self._reversal_threshold = signal_reversal_threshold

    async def tick(self) -> list[Position]:
        """
        Check all open positions. Returns list of positions that were closed.

        Fetches a side-aware mark per position: for a LONG, the mark is the
        current bid (what we'd receive on close); for a SHORT, the ask.
        This replaces the previous last-trade ticker which could hide
        the real exit price during fast moves.
        """
        closed: list[Position] = []

        for position in self._portfolio.open_positions:
            # Real close-side mark for this position — not the last-trade ticker
            mark = await self._exchange.get_mark(
                f"{position.asset}USDT", position.side,
            )
            exit_reason = await self._evaluate_exit(position, mark)
            if exit_reason is not None:
                closed_pos = await self._close_position(position, exit_reason)
                if closed_pos:
                    closed.append(closed_pos)
            else:
                # Update trailing stop if price moved favourably
                self._update_trailing_stop(position, mark.value)

        return closed

    async def _evaluate_exit(
        self,
        position: Position,
        mark: Price,
    ) -> Optional[ExitReason]:
        """Determine if a position should be closed and why.

        `mark` is the close-side price (bid for LONG, ask for SHORT). All
        price-based checks compare against this, so stops and take-profits
        fire at the level we'd actually cross.
        """
        price = mark.value

        # 1. Stop-loss
        if position.should_stop_loss(price):
            return ExitReason.STOP_LOSS

        # 2. Take-profit
        if position.should_take_profit(price):
            return ExitReason.TAKE_PROFIT

        # 3. Max hold time
        if position.is_expired():
            return ExitReason.MAX_HOLD_TIME

        # 4. Signal reversal — check if composite score has flipped.
        # Note: this is signal-driven, not price-driven. It fires even if the
        # position is at small profit. Planned follow-up: P&L-protected
        # reversal exit that uses exchange.get_unrealised_pnl(position) to
        # skip the exit when we're comfortably in the money.
        signal = await self._signal_port.get_latest_signal(position.entry_timescale)
        if signal is not None:
            if position.side == TradeSide.LONG and signal.score < self._reversal_threshold:
                return ExitReason.SIGNAL_REVERSAL
            elif position.side == TradeSide.SHORT and signal.score > -self._reversal_threshold:
                return ExitReason.SIGNAL_REVERSAL

        return None

    async def _close_position(
        self,
        position: Position,
        reason: ExitReason,
    ) -> Optional[Position]:
        """Execute position close on exchange."""
        try:
            position.request_exit(reason)

            fill = await self._exchange.close_position(
                symbol=f"{position.asset}USDT",
                side=position.side,
                notional=position.notional,
            )

            position.confirm_exit(
                price=fill.fill_price,
                order_id=fill.order_id,
                commission=fill.commission,
                commission_is_actual=fill.commission_is_actual,
            )
            self._portfolio.on_position_closed(position)
            await self._repo.save(position)
            await self._alerts.send_trade_closed(position)

            logger.info(
                "Position closed: %s %s @ %.2f, PnL=%.2f, "
                "commission=%.4f (actual=%s), reason=%s",
                position.side.value, position.asset,
                fill.fill_price.value, position.realised_pnl,
                fill.commission, fill.commission_is_actual, reason.value,
            )
            return position

        except Exception as e:
            logger.error("Failed to close position %s: %s", position.id, e)
            # Revert state — position is still OPEN
            position.state = PositionState.OPEN
            position.exit_reason = None
            await self._alerts.send_error(f"Close failed for {position.id}: {e}")
            return None

    def _update_trailing_stop(self, position: Position, current_price: float) -> None:
        """Update trailing stop if price has moved favourably."""
        if not position.trailing_stop or not position.trailing_stop.is_trailing:
            return

        trail_pct = position.trailing_stop.trail_pct

        if position.side == TradeSide.LONG:
            new_stop = current_price * (1 - trail_pct)
            if new_stop > position.trailing_stop.price:
                position.trailing_stop = StopLevel(
                    price=new_stop,
                    is_trailing=True,
                    trail_pct=trail_pct,
                )
                # Also update the main stop_loss to the trailing level
                position.stop_loss = StopLevel(price=new_stop)
        else:
            new_stop = current_price * (1 + trail_pct)
            if new_stop < position.trailing_stop.price:
                position.trailing_stop = StopLevel(
                    price=new_stop,
                    is_trailing=True,
                    trail_pct=trail_pct,
                )
                position.stop_loss = StopLevel(price=new_stop)
