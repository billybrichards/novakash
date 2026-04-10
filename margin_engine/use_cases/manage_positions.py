"""
Use case: Manage open positions — price and time-based exits only.

v2 (April 2026) removed the SIGNAL_REVERSAL exit path. In v1, 100% of 195
closed positions exited via SIGNAL_REVERSAL, all at a loss exactly equal
to the round-trip fee cost. The mechanism was deterministic: the v3
composite oscillates on a ~4-minute cycle, so a position opened on a
signal peak would see the composite revert through zero within its
holding window and trip signal_reversal_threshold, exiting at the fee
wall. Removing this exit lets the price thesis actually play out.

Exit precedence (evaluated in order, first match wins):
  1. Stop-loss hit          → STOP_LOSS
  2. Take-profit hit        → TAKE_PROFIT
  3. Max hold time expired  → MAX_HOLD_TIME
  4. (SIGNAL_REVERSAL removed — do not add it back)

The trailing stop is still updated when price moves favourably, but it
only fires via the STOP_LOSS branch above — there's no separate
TRAILING_STOP exit reason emitted.
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
)
from margin_engine.domain.value_objects import (
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
        trailing_stop_pct: float = 0.003,   # 0.3%, matched to 0.6% SL
    ) -> None:
        self._exchange = exchange
        self._portfolio = portfolio
        self._repo = repository
        self._alerts = alerts
        self._trailing_pct = trailing_stop_pct

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

        v2 change: no SIGNAL_REVERSAL branch. In v1 this was responsible
        for 100% of 195 closed positions, all losing trades at the fee
        wall. The calibrated ML probability used in OpenPositionUseCase
        already encodes the expected reversal probability for the
        forecast horizon, so using the composite as an interim exit
        signal was double-counting noise.
        """
        price = mark.value

        # 1. Stop-loss
        if position.should_stop_loss(price):
            return ExitReason.STOP_LOSS

        # 2. Take-profit
        if position.should_take_profit(price):
            return ExitReason.TAKE_PROFIT

        # 3. Max hold time — this is the primary time-based exit. For
        # 15m-horizon trades the max_hold_seconds should be set close to
        # the window close (around 900s) so the position naturally exits
        # when the prediction horizon expires. This IS the "exit when the
        # forecast is resolved" behaviour, in the language the Position
        # entity already speaks.
        if position.is_expired():
            return ExitReason.MAX_HOLD_TIME

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
