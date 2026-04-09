"""
Domain value objects — immutable, validated, self-describing types.

These are the building blocks that entities and use cases compose.
All validation happens at construction time; once created, a value object
is guaranteed to be in a valid state.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import Optional


class TradeSide(enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def opposite(self) -> TradeSide:
        return TradeSide.SHORT if self is TradeSide.LONG else TradeSide.LONG


class ExitReason(enum.Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"
    MAX_HOLD_TIME = "MAX_HOLD_TIME"
    MANUAL = "MANUAL"
    KILL_SWITCH = "KILL_SWITCH"


class PositionState(enum.Enum):
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    PENDING_EXIT = "PENDING_EXIT"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class Money:
    """
    Non-negative monetary amount with currency.

    Immutable. Arithmetic returns new Money instances.
    Negative amounts are forbidden — use signed floats for P&L deltas.
    """
    amount: float
    currency: str = "USDT"

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError(f"Money cannot be negative: {self.amount}")
        if math.isnan(self.amount) or math.isinf(self.amount):
            raise ValueError(f"Money must be finite: {self.amount}")

    def __add__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise ValueError(f"Cannot add {self.currency} + {other.currency}")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise ValueError(f"Cannot subtract {self.currency} - {other.currency}")
        result = self.amount - other.amount
        if result < 0:
            raise ValueError(f"Money subtraction would go negative: {self.amount} - {other.amount}")
        return Money(result, self.currency)

    def __mul__(self, factor: float) -> Money:
        if factor < 0:
            raise ValueError(f"Cannot multiply Money by negative factor: {factor}")
        return Money(self.amount * factor, self.currency)

    def __gt__(self, other: Money) -> bool:
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        return self.amount >= other.amount

    def __lt__(self, other: Money) -> bool:
        return self.amount < other.amount

    @classmethod
    def zero(cls, currency: str = "USDT") -> Money:
        return cls(0.0, currency)

    @classmethod
    def usd(cls, amount: float) -> Money:
        return cls(amount, "USDT")


@dataclass(frozen=True)
class Price:
    """Validated price — must be positive and finite."""
    value: float
    pair: str = "BTCUSDT"

    def __post_init__(self) -> None:
        if self.value <= 0 or math.isnan(self.value) or math.isinf(self.value):
            raise ValueError(f"Price must be positive and finite: {self.value}")


@dataclass(frozen=True)
class CompositeSignal:
    """
    Validated composite signal from the v3 scorer.
    score must be in [-1, +1], timescale must be one of the 9 defined.
    """
    score: float
    timescale: str
    asset: str = "BTC"
    timestamp: float = 0.0

    VALID_TIMESCALES = ("5m", "15m", "1h", "4h", "24h", "48h", "72h", "1w", "2w")

    def __post_init__(self) -> None:
        if not -1.0 <= self.score <= 1.0:
            raise ValueError(f"CompositeSignal score must be in [-1, 1]: {self.score}")
        if self.timescale not in self.VALID_TIMESCALES:
            raise ValueError(f"Invalid timescale: {self.timescale}")

    @property
    def is_bullish(self) -> bool:
        return self.score > 0

    @property
    def is_bearish(self) -> bool:
        return self.score < 0

    @property
    def strength(self) -> float:
        """Absolute signal strength [0, 1]."""
        return abs(self.score)

    @property
    def suggested_side(self) -> TradeSide:
        return TradeSide.LONG if self.is_bullish else TradeSide.SHORT


@dataclass(frozen=True)
class StopLevel:
    """Stop-loss or take-profit level with price and type."""
    price: float
    is_trailing: bool = False
    trail_pct: float = 0.0

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError(f"StopLevel price must be positive: {self.price}")
        if self.is_trailing and self.trail_pct <= 0:
            raise ValueError(f"Trailing stop needs positive trail_pct: {self.trail_pct}")
