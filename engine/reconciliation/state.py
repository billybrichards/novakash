"""Pure dataclasses for CLOB reconciler state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class WalletSnapshot:
    """Point-in-time USDC balance from CLOB API."""

    balance_usdc: float
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class OpenPosition:
    """A single on-chain position from Polymarket data API."""

    condition_id: str
    token_id: str
    size: float
    avg_price: float
    cost: float
    value: float
    pnl: float
    outcome: str  # "WIN", "LOSS", or "OPEN"


@dataclass
class RestingOrder:
    """A resting GTC order on the CLOB order book."""

    order_id: str
    token_id: str
    price: float
    size_original: float
    size_matched: float
    status: str
    created_at: Optional[datetime] = None


@dataclass
class ReconcilerState:
    """Full reconciler state snapshot."""

    wallet: Optional[WalletSnapshot] = None
    positions: list[OpenPosition] = field(default_factory=list)
    resting_orders: list[RestingOrder] = field(default_factory=list)
    last_poll_at: Optional[datetime] = None
    last_report_at: Optional[datetime] = None
