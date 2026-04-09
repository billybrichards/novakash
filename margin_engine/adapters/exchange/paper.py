"""
Paper exchange adapter — simulates Binance margin without real orders.

Tracks a virtual balance, simulates fills at current price + slippage,
and maintains position state. Used for testing and paper trading.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

from margin_engine.domain.ports import ExchangePort
from margin_engine.domain.value_objects import Money, Price, TradeSide

logger = logging.getLogger(__name__)


class PaperExchangeAdapter(ExchangePort):
    """
    Paper trading adapter — no real orders, simulated fills.

    Uses a real price feed for current prices but simulates order
    execution with configurable slippage.
    """

    def __init__(
        self,
        starting_balance: float = 500.0,
        slippage_bps: float = 2.0,
        fee_rate: float = 0.00075,
        price_getter=None,  # callable returning current BTC price
    ) -> None:
        self._balance = starting_balance
        self._slippage_bps = slippage_bps
        self._fee_rate = fee_rate
        self._price_getter = price_getter
        self._order_counter = 0
        self._last_price = 80000.0  # reasonable default

    async def place_market_order(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> tuple[str, Price]:
        """Simulate a market order with slippage."""
        price = await self.get_current_price(symbol)
        slippage = price.value * self._slippage_bps / 10000

        if side == TradeSide.LONG:
            fill_price = price.value + slippage  # pay slightly more
        else:
            fill_price = price.value - slippage  # receive slightly less

        fee = notional.amount * self._fee_rate
        self._balance -= fee

        self._order_counter += 1
        order_id = f"PAPER-{self._order_counter}"

        logger.info(
            "PAPER order: %s %s %.2f USDT @ %.2f (slippage=%.2f, fee=%.4f)",
            side.value, symbol, notional.amount, fill_price, slippage, fee,
        )
        return order_id, Price(value=fill_price, pair=symbol)

    async def close_position(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> tuple[str, Price]:
        """Simulate closing a position."""
        price = await self.get_current_price(symbol)
        slippage = price.value * self._slippage_bps / 10000

        # Closing a LONG = selling (receive less), closing a SHORT = buying (pay more)
        if side == TradeSide.LONG:
            fill_price = price.value - slippage
        else:
            fill_price = price.value + slippage

        fee = notional.amount * self._fee_rate
        self._balance -= fee

        self._order_counter += 1
        order_id = f"PAPER-{self._order_counter}"

        logger.info(
            "PAPER close: %s %s @ %.2f (fee=%.4f)",
            side.value, symbol, fill_price, fee,
        )
        return order_id, Price(value=fill_price, pair=symbol)

    async def get_balance(self) -> Money:
        return Money.usd(max(0.0, self._balance))

    async def get_current_price(self, symbol: str) -> Price:
        if self._price_getter:
            try:
                price = self._price_getter()
                if price and price > 0:
                    self._last_price = price
                    return Price(value=price, pair=symbol)
            except Exception:
                pass
        return Price(value=self._last_price, pair=symbol)

    def adjust_balance(self, delta: float) -> None:
        """Manually adjust paper balance (for P&L tracking)."""
        self._balance += delta
