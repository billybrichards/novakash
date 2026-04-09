"""
Use case: Open a new margin position.

Orchestrates: signal validation → risk check → order placement → position tracking.
Depends only on domain ports — never imports adapters directly.
"""
from __future__ import annotations

import logging
from typing import Optional

from margin_engine.domain.entities.portfolio import Portfolio
from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import AlertPort, ExchangePort, PositionRepository
from margin_engine.domain.value_objects import (
    CompositeSignal,
    Money,
    Price,
    PositionState,
    StopLevel,
    TradeSide,
)

logger = logging.getLogger(__name__)


class OpenPositionUseCase:
    """
    Evaluates a composite signal and opens a margin position if conditions are met.

    Entry criteria:
      1. Signal strength > threshold (configurable)
      2. Portfolio risk gates pass
      3. Exchange order fills successfully
    """

    def __init__(
        self,
        exchange: ExchangePort,
        portfolio: Portfolio,
        repository: PositionRepository,
        alerts: AlertPort,
        signal_threshold: float = 0.3,
        bet_fraction: float = 0.05,
        stop_loss_pct: float = 0.015,
        take_profit_pct: float = 0.03,
    ) -> None:
        self._exchange = exchange
        self._portfolio = portfolio
        self._repo = repository
        self._alerts = alerts
        self._signal_threshold = signal_threshold
        self._bet_fraction = bet_fraction
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct

    async def execute(self, signal: CompositeSignal) -> Optional[Position]:
        """
        Evaluate signal and open position if criteria are met.
        Returns the opened Position, or None if skipped.
        """
        # 1. Signal strength check
        if signal.strength < self._signal_threshold:
            logger.info(
                "Signal too weak: %.3f < %.3f (%s)",
                signal.strength, self._signal_threshold, signal.timescale,
            )
            return None

        # 2. Compute position size
        balance = await self._exchange.get_balance()
        collateral = Money.usd(balance.amount * self._bet_fraction)
        notional = collateral * self._portfolio.leverage

        # 3. Portfolio risk gate
        allowed, reason = self._portfolio.can_open_position(collateral)
        if not allowed:
            logger.info("Position blocked by risk gate: %s", reason)
            return None

        # 4. Get current price for stop levels
        current_price = await self._exchange.get_current_price("BTCUSDT")
        side = signal.suggested_side

        # 5. Compute stop levels
        stop_loss = self._compute_stop_loss(current_price, side)
        take_profit = self._compute_take_profit(current_price, side)

        # 6. Create position entity
        position = Position(
            asset=signal.asset,
            side=side,
            leverage=self._portfolio.leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_signal_score=signal.score,
            entry_timescale=signal.timescale,
        )
        self._portfolio.add_position(position)

        # 7. Place order
        try:
            order_id, fill_price = await self._exchange.place_market_order(
                symbol=f"{signal.asset}USDT",
                side=side,
                notional=notional,
            )
            position.confirm_entry(fill_price, notional, collateral, order_id)
            await self._repo.save(position)
            await self._alerts.send_trade_opened(position)
            logger.info(
                "Position opened: %s %s @ %.2f, notional=%.2f, signal=%.3f",
                side.value, signal.asset, fill_price.value, notional.amount, signal.score,
            )
            return position

        except Exception as e:
            logger.error("Order placement failed: %s", e)
            await self._alerts.send_error(f"Order failed: {e}")
            # Remove from portfolio since it never filled
            self._portfolio.positions.remove(position)
            return None

    def _compute_stop_loss(self, price: Price, side: TradeSide) -> StopLevel:
        if side == TradeSide.LONG:
            return StopLevel(price=price.value * (1 - self._stop_loss_pct))
        else:
            return StopLevel(price=price.value * (1 + self._stop_loss_pct))

    def _compute_take_profit(self, price: Price, side: TradeSide) -> StopLevel:
        if side == TradeSide.LONG:
            return StopLevel(price=price.value * (1 + self._take_profit_pct))
        else:
            return StopLevel(price=price.value * (1 - self._take_profit_pct))
