"""
Paper exchange adapter — simulates Binance margin without real orders.

Tracks a virtual balance, simulates fills at current price + slippage,
and maintains position state. Used for testing and paper trading.

Design goal: paper must match the SHAPE of live mode as closely as possible.
If live mode computes stops from bid/ask and reads real commissions off fills,
paper does the same using modeled values. This prevents the class of bug
from tasks/lessons.md where paper and live diverged silently.
"""

from __future__ import annotations

import logging
from typing import Optional

from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import ExchangePort
from margin_engine.domain.value_objects import Money, Price, TradeSide
from margin_engine.domain.ports import FillResult

logger = logging.getLogger(__name__)


class PaperExchangeAdapter(ExchangePort):
    """
    Paper trading adapter — no real orders, simulated fills.

    Uses a real price feed for current prices but simulates order
    execution with a modeled bid/ask spread and a fee rate. The spread
    is symmetric around the "current price" returned by the price getter;
    LONG orders cross the ask on entry and sell into the bid on exit,
    which mirrors how live Binance behaves.
    """

    def __init__(
        self,
        starting_balance: float = 500.0,
        spread_bps: float = 2.0,  # 2bp total spread — realistic for BTCUSDT top of book
        fee_rate: float = 0.001,  # 0.1% per side — matches live fallback
        price_getter=None,  # callable returning current BTC mid price
    ) -> None:
        self._balance = starting_balance
        self._spread_bps = spread_bps
        self._fee_rate = fee_rate
        self._price_getter = price_getter
        self._order_counter = 0
        self._last_price = 80000.0  # reasonable default

    # ─── Internal ────────────────────────────────────────────────────────

    def _bid_ask(self, mid: float) -> tuple[float, float]:
        """Return (bid, ask) around a mid price using the configured spread."""
        half = mid * self._spread_bps / 10000 / 2
        return mid - half, mid + half

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"PAPER-{self._order_counter}"

    # ─── ExchangePort implementation ─────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> FillResult:
        """Simulate a market order with bid/ask slippage."""
        mid = (await self.get_current_price(symbol)).value
        bid, ask = self._bid_ask(mid)

        # Opening LONG = buy at ask; opening SHORT = sell at bid
        fill_price = ask if side == TradeSide.LONG else bid

        fee = notional.amount * self._fee_rate
        self._balance -= fee

        order_id = self._next_order_id()
        logger.info(
            "PAPER order: %s %s %.2f USDT @ %.2f (mid=%.2f, fee=%.4f)",
            side.value,
            symbol,
            notional.amount,
            fill_price,
            mid,
            fee,
        )
        return FillResult(
            order_id=order_id,
            fill_price=Price(value=fill_price, pair=symbol),
            filled_notional=notional.amount,
            commission=fee,
            commission_asset="USDT",
            commission_is_actual=True,  # in paper, our calculation IS the outcome
        )

    async def close_position(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> FillResult:
        """Simulate closing a position (opposite side crosses the book)."""
        mid = (await self.get_current_price(symbol)).value
        bid, ask = self._bid_ask(mid)

        # Closing a LONG = sell at bid; closing a SHORT = buy at ask
        fill_price = bid if side == TradeSide.LONG else ask

        fee = notional.amount * self._fee_rate
        self._balance -= fee

        order_id = self._next_order_id()
        logger.info(
            "PAPER close: %s %s %.2f USDT @ %.2f (mid=%.2f, fee=%.4f)",
            side.value,
            symbol,
            notional.amount,
            fill_price,
            mid,
            fee,
        )
        return FillResult(
            order_id=order_id,
            fill_price=Price(value=fill_price, pair=symbol),
            filled_notional=notional.amount,
            commission=fee,
            commission_asset="USDT",
            commission_is_actual=True,
        )

    async def get_balance(self) -> Money:
        return Money.usd(max(0.0, self._balance))

    async def get_current_price(self, symbol: str) -> Price:
        """Return the mid price. Use get_mark() when you need bid/ask."""
        if self._price_getter:
            try:
                price = self._price_getter()
                if price and price > 0:
                    self._last_price = price
                    return Price(value=price, pair=symbol)
            except Exception:
                pass
        return Price(value=self._last_price, pair=symbol)

    async def get_mark(self, symbol: str, side: TradeSide) -> Price:
        """Side-aware close mark using the modeled spread.

        Mirrors live Binance behavior: LONG closes into the bid, SHORT closes
        into the ask. Stop-loss and take-profit evaluation should go through
        this, not get_current_price.
        """
        mid = (await self.get_current_price(symbol)).value
        bid, ask = self._bid_ask(mid)
        price = bid if side == TradeSide.LONG else ask
        return Price(value=price, pair=symbol)

    async def get_unrealised_pnl(self, position: Position) -> float:
        """Unrealised P&L at the modeled close-side mark.

        Delegates to Position.unrealised_pnl_net which factors in the real
        entry commission we stored on open, an estimated exit commission,
        and estimated borrow interest. Same calculation as live mode —
        only the mark differs.
        """
        if not position.asset:
            return 0.0
        mark = await self.get_mark(f"{position.asset}USDT", position.side)
        return position.unrealised_pnl_net(mark.value)

    def adjust_balance(self, delta: float) -> None:
        """Manually adjust paper balance (for P&L tracking)."""
        self._balance += delta
