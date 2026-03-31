"""
Order Manager — Order Lifecycle Tracking

Maintains an in-memory registry of all open and recently closed orders/bets
across both Polymarket and Opinion venues.

Responsibilities:
  - Track order state: PENDING → FILLED | CANCELLED | EXPIRED
  - Compute per-order PnL when bets resolve
  - Persist resolved orders to DB via db_client
  - Enforce MAX_OPEN_EXPOSURE_PCT by tracking total open exposure
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
import structlog

from config.constants import MAX_OPEN_EXPOSURE_PCT
from persistence.db_client import DBClient

log = structlog.get_logger(__name__)


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    RESOLVED_WIN = "RESOLVED_WIN"
    RESOLVED_LOSS = "RESOLVED_LOSS"


@dataclass
class Order:
    """Represents a single bet/order on a prediction market."""
    order_id: str
    strategy: str       # "arb" | "vpin_cascade"
    venue: str          # "polymarket" | "opinion"
    market_slug: str
    direction: str      # "YES" | "NO" | "ARB" (both legs)
    entry_price: Decimal
    stake_usd: float
    fee_usd: float
    status: OrderStatus = OrderStatus.PENDING
    outcome: Optional[str] = None   # "WIN" | "LOSS" | "PUSH"
    payout_usd: Optional[float] = None
    pnl_usd: Optional[float] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)


class OrderManager:
    """
    Manages the full lifecycle of prediction market orders.

    Keeps all open orders in memory; persists on resolution.
    """

    def __init__(self, db: DBClient, bankroll: float) -> None:
        self._db = db
        self._bankroll = bankroll
        self._orders: dict[str, Order] = {}
        self._lock = asyncio.Lock()

    async def register_order(self, order: Order) -> None:
        """Register a new order after placement."""
        async with self._lock:
            self._orders[order.order_id] = order
        log.info("order.registered", id=order.order_id, strategy=order.strategy, stake=order.stake_usd)

    async def resolve_order(
        self,
        order_id: str,
        outcome: str,
        payout_usd: float,
    ) -> Order:
        """
        Mark an order as resolved and compute PnL.

        Args:
            order_id: The order to resolve.
            outcome: "WIN", "LOSS", or "PUSH".
            payout_usd: Amount received on resolution.
        """
        async with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise KeyError(f"Order {order_id} not found")

            order.outcome = outcome
            order.payout_usd = payout_usd
            order.pnl_usd = payout_usd - order.stake_usd - order.fee_usd
            order.status = OrderStatus.RESOLVED_WIN if outcome == "WIN" else OrderStatus.RESOLVED_LOSS
            order.resolved_at = datetime.utcnow()

        log.info(
            "order.resolved",
            id=order_id,
            outcome=outcome,
            pnl=order.pnl_usd,
        )

        # Persist to database
        await self._db.save_trade(order)
        return order

    async def get_open_exposure_usd(self) -> float:
        """Return total USD currently at risk in open orders."""
        async with self._lock:
            return sum(
                o.stake_usd
                for o in self._orders.values()
                if o.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.FILLED)
            )

    async def can_open_position(self, stake_usd: float) -> bool:
        """Check whether opening a new position would breach exposure limits."""
        current_exposure = await self.get_open_exposure_usd()
        max_exposure = self._bankroll * MAX_OPEN_EXPOSURE_PCT
        return (current_exposure + stake_usd) <= max_exposure

    async def get_open_orders(self) -> list[Order]:
        """Return all currently open orders."""
        async with self._lock:
            return [
                o for o in self._orders.values()
                if o.status in (OrderStatus.PENDING, OrderStatus.OPEN)
            ]

    async def update_bankroll(self, new_bankroll: float) -> None:
        """Update bankroll reference (called by risk manager)."""
        self._bankroll = new_bankroll

    def get_all_orders(self) -> dict[str, Order]:
        """Return a snapshot of all orders (open + closed)."""
        return dict(self._orders)
