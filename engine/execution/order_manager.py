"""
OrderManager — tracks open orders and handles paper-mode resolution polling.

Resolution logic (paper mode):
- Polymarket orders expire after 5 minutes (POLY_WINDOW_SECONDS).
- Opinion orders expire after 15 minutes (OPINION_WINDOW_SECONDS).
- Arb strategy orders always resolve as WIN (guaranteed profit).
- vpin_cascade strategy orders resolve WIN if BTC moved in the predicted direction.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

# Window lengths for paper-mode expiry
POLY_WINDOW_SECONDS: int = 300    # 5 minutes
OPINION_WINDOW_SECONDS: int = 900  # 15 minutes

# Minimum BTC price move to count as directional win (paper mode)
MIN_BTC_MOVE_PCT: float = 0.001  # 0.1%


class OrderStatus(str, Enum):
    """Lifecycle states for a tracked order."""
    OPEN = "OPEN"
    FILLED = "FILLED"
    RESOLVED_WIN = "RESOLVED_WIN"
    RESOLVED_LOSS = "RESOLVED_LOSS"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass
class Order:
    """Represents a single order across either venue.

    Attributes:
        order_id: Venue-assigned (or paper) order identifier.
        venue: "polymarket" or "opinion".
        strategy: Strategy that placed the order, e.g. "arb", "vpin_cascade".
        direction: "YES" or "NO".
        price: Fill price as a decimal string (e.g. "0.5123").
        stake_usd: USD risked.
        status: Current lifecycle state.
        created_at: Unix timestamp of order creation.
        resolved_at: Unix timestamp of resolution (None if still open).
        outcome: "WIN" or "LOSS" after resolution.
        payout_usd: USD received on resolution.
        btc_entry_price: BTC/USD price at the time of order placement.
        window_seconds: Duration of the prediction window in seconds.
        market_id: Venue-specific market identifier.
    """
    order_id: str
    venue: str                       # "polymarket" | "opinion"
    strategy: str                    # "arb" | "vpin_cascade" | ...
    direction: str                   # "YES" | "NO"
    price: str
    stake_usd: float
    status: OrderStatus = OrderStatus.OPEN
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    outcome: Optional[str] = None    # "WIN" | "LOSS"
    payout_usd: Optional[float] = None
    pnl_usd: Optional[float] = None
    fee_usd: float = 0.0
    btc_entry_price: Optional[float] = None
    window_seconds: int = POLY_WINDOW_SECONDS
    market_id: str = ""
    metadata: dict = field(default_factory=dict)


class OrderManager:
    """
    Manages the full lifecycle of orders from placement to resolution.

    Thread-safety: an asyncio Lock guards all state mutations so the
    manager is safe to use from concurrent coroutines.
    """

    def __init__(
        self,
        db: object = None,
        bankroll: float = 500.0,
        paper_mode: bool = True,
        on_resolution: Optional[callable] = None,
    ) -> None:
        self._db = db
        self._bankroll = bankroll
        self._paper_mode = paper_mode
        self._on_resolution = on_resolution
        self._orders: Dict[str, Order] = {}
        self._lock = asyncio.Lock()
        self._log = logger.bind(component="order_manager")
        self._current_btc_price: float = 0.0

        # Counters exposed to RiskManager
        self._total_orders: int = 0
        self._resolved_orders: int = 0

    def update_btc_price(self, price) -> None:
        """Update the current BTC price (called by orchestrator on each trade)."""
        self._current_btc_price = float(price)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register_order(self, order: Order) -> None:
        """Add a newly-placed order to tracking.

        Args:
            order: The Order dataclass instance to track.
        """
        async with self._lock:
            self._orders[order.order_id] = order
            self._total_orders += 1
            self._log.info(
                "order_manager.registered",
                order_id=order.order_id,
                venue=order.venue,
                strategy=order.strategy,
                direction=order.direction,
                stake_usd=order.stake_usd,
            )

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    async def resolve_order(
        self,
        order_id: str,
        outcome: str,
        payout_usd: float,
    ) -> Order:
        """Mark an order as resolved with a known outcome.

        Args:
            order_id: Order to resolve.
            outcome: "WIN" or "LOSS".
            payout_usd: USD received (0 for a loss, > stake_usd for win).

        Returns:
            The updated Order.

        Raises:
            KeyError: If order_id is unknown.
            ValueError: If outcome is not WIN or LOSS.
        """
        if outcome not in {"WIN", "LOSS"}:
            raise ValueError(f"outcome must be WIN or LOSS, got {outcome!r}")

        async with self._lock:
            order = self._orders[order_id]  # raises KeyError if missing
            order.status = (
                OrderStatus.RESOLVED_WIN if outcome == "WIN" else OrderStatus.RESOLVED_LOSS
            )
            order.outcome = outcome
            order.payout_usd = payout_usd
            order.pnl_usd = payout_usd - order.stake_usd - order.fee_usd
            order.resolved_at = time.time()
            self._resolved_orders += 1

            pnl = order.pnl_usd
            self._log.info(
                "order_manager.resolved",
                order_id=order_id,
                outcome=outcome,
                payout_usd=payout_usd,
                pnl=pnl,
                venue=order.venue,
                strategy=order.strategy,
            )
            return order

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_open_orders(self) -> List[Order]:
        """Return all orders currently in OPEN or FILLED state."""
        async with self._lock:
            return [
                o for o in self._orders.values()
                if o.status in {OrderStatus.OPEN, OrderStatus.FILLED}
            ]

    async def get_open_exposure_usd(self) -> float:
        """Return total USD at risk across all open orders."""
        open_orders = await self.get_open_orders()
        return sum(o.stake_usd for o in open_orders)

    @property
    def total_orders(self) -> int:
        """Total orders registered (all time)."""
        return self._total_orders

    @property
    def resolved_orders(self) -> int:
        """Total orders resolved (WIN or LOSS)."""
        return self._resolved_orders

    # ------------------------------------------------------------------
    # Paper-mode resolution polling
    # ------------------------------------------------------------------

    async def poll_resolutions(self, btc_price: Optional[float] = None) -> None:
        """Check open orders and auto-resolve expired ones (paper mode).

        Called periodically by the engine's main loop with the current
        BTC spot price.

        Resolution rules:
        - Polymarket orders expire after ``window_seconds`` (default 5 min).
        - Opinion orders expire after 15 minutes.
        - ``arb`` strategy: always WIN (guaranteed profit from price discrepancy).
        - ``vpin_cascade`` and others: WIN if BTC moved in the predicted direction
          by at least MIN_BTC_MOVE_PCT from ``btc_entry_price``.

        Args:
            btc_price: Current BTC/USD spot price (uses stored price if None).
        """
        price = btc_price if btc_price is not None else self._current_btc_price
        if price <= 0:
            return

        now = time.time()
        expired: List[Order] = []

        async with self._lock:
            for order in self._orders.values():
                if order.status not in {OrderStatus.OPEN, OrderStatus.FILLED}:
                    continue
                age = now - order.created_at
                if age >= order.window_seconds:
                    expired.append(order)

        for order in expired:
            outcome, payout = self._determine_paper_outcome(order, price)
            resolved = await self.resolve_order(order.order_id, outcome, payout)
            if self._on_resolution:
                try:
                    self._on_resolution(resolved)
                except Exception as exc:
                    self._log.error("order_manager.resolution_callback_error", error=str(exc))

    def _determine_paper_outcome(
        self, order: Order, current_btc_price: float
    ) -> tuple[str, float]:
        """Determine WIN/LOSS and payout for an expired paper order.

        Args:
            order: The expired order.
            current_btc_price: Current BTC/USD price.

        Returns:
            Tuple of (outcome: str, payout_usd: float).
        """
        if order.strategy == "arb":
            # Arb is always a WIN — the profit is baked into the entry
            payout = order.stake_usd * 1.15  # ~15% guaranteed return simulation
            self._log.debug(
                "paper_resolution.arb_win",
                order_id=order.order_id,
                payout=payout,
            )
            return "WIN", payout

        # For vpin_cascade and other directional strategies, check BTC move
        entry = order.btc_entry_price
        if entry is None or entry == 0.0:
            # No entry price recorded — treat as coin-flip
            import random
            outcome = "WIN" if random.random() > 0.5 else "LOSS"
            payout = order.stake_usd * 1.9 if outcome == "WIN" else 0.0
            return outcome, payout

        move_pct = (current_btc_price - entry) / entry

        # Map direction to expected move
        if order.direction == "YES":
            # YES bet = BTC goes UP
            won = move_pct >= MIN_BTC_MOVE_PCT
        else:
            # NO bet = BTC goes DOWN
            won = move_pct <= -MIN_BTC_MOVE_PCT

        if won:
            # Approximate payout based on fill price (binary payout = 1/price per share)
            try:
                fill_price = float(order.price)
                shares = order.stake_usd / fill_price if fill_price > 0 else 0
                payout = shares * 1.0  # Binary market pays $1 per winning share
            except (ValueError, ZeroDivisionError):
                payout = order.stake_usd * 1.9
            self._log.debug(
                "paper_resolution.direction_win",
                order_id=order.order_id,
                direction=order.direction,
                move_pct=f"{move_pct*100:.3f}%",
                payout=payout,
            )
            return "WIN", payout
        else:
            self._log.debug(
                "paper_resolution.direction_loss",
                order_id=order.order_id,
                direction=order.direction,
                move_pct=f"{move_pct*100:.3f}%",
            )
            return "LOSS", 0.0
