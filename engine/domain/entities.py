"""Domain entities — mutable stateful objects with lifecycle.

Order is the primary domain entity: it tracks the lifecycle of a single trade
(OPEN → FILLED/EXPIRED/CANCELLED/FAILED) and its fields are mutated by
OrderManager as the order progresses.

Contrast with value_objects.py (frozen dataclasses, no identity).
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from enum import Enum as _Enum
from typing import Optional

__all__ = ["Order", "OrderStatus"]

# Default window lengths — authoritative values live in config.constants.
# Duplicated here to keep the domain layer free of config dependencies
# (domain is the innermost layer; config imports settings which needs env vars).
_POLY_WINDOW_SECONDS_DEFAULT: int = 300   # 5 minutes
_OPINION_WINDOW_SECONDS_DEFAULT: int = 900  # 15 minutes


class OrderStatus(str, _Enum):
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
    created_at: float = field(default_factory=_time.time)
    resolved_at: Optional[float] = None
    outcome: Optional[str] = None    # "WIN" | "LOSS"
    payout_usd: Optional[float] = None
    pnl_usd: Optional[float] = None
    fee_usd: float = 0.0
    btc_entry_price: Optional[float] = None
    window_seconds: int = _POLY_WINDOW_SECONDS_DEFAULT
    market_id: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def market_slug(self) -> str:
        """Alias for market_id — used by db_client.write_trade."""
        return self.market_id

    @property
    def entry_price(self) -> str:
        """Alias for price — used by db_client.write_trade."""
        return self.price
