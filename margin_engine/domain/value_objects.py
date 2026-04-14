"""
Domain value objects — immutable, validated, self-describing types.

These are the building blocks that entities and use cases compose.
All validation happens at construction time; once created, a value object
is guaranteed to be in a valid state.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Optional

from margin_engine.domain.exceptions import DomainValidationError


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
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"  # legacy composite path, kept for old rows
    MAX_HOLD_TIME = "MAX_HOLD_TIME"  # legacy exit, used as v2 fallback
    MANUAL = "MANUAL"
    KILL_SWITCH = "KILL_SWITCH"
    # ── v4-aware exit reasons (PR B) ──
    # Distinct codes so telemetry can separate WHICH gate killed the trade.
    # PROBABILITY_REVERSAL and REGIME_DETERIORATED describe model/market
    # state changes; CONSENSUS_FAIL and MACRO_GATE_FLIP describe external
    # hard-gate flips; EVENT_GUARD and CASCADE_EXHAUSTED are preemptive
    # exits before something predictable happens.
    PROBABILITY_REVERSAL = "PROBABILITY_REVERSAL"  # p_up flipped or conviction too low
    REGIME_DETERIORATED = "REGIME_DETERIORATED"  # regime became CHOPPY / NO_EDGE
    CONSENSUS_FAIL = "CONSENSUS_FAIL"  # 6-source oracle divergence spiked
    MACRO_GATE_FLIP = "MACRO_GATE_FLIP"  # Claude flipped direction_gate against us
    EVENT_GUARD = "EVENT_GUARD"  # forced exit before HIGH/EXTREME event
    CASCADE_EXHAUSTED = "CASCADE_EXHAUSTED"  # cascade FSM says about to reverse


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
        errors = []
        if self.amount < 0:
            errors.append(f"Money cannot be negative: {self.amount}")
        if math.isnan(self.amount) or math.isinf(self.amount):
            errors.append(f"Money must be finite: {self.amount}")
        if errors:
            raise DomainValidationError(errors)

    def __add__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise DomainValidationError(
                [f"Cannot add {self.currency} + {other.currency}"]
            )
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise DomainValidationError(
                [f"Cannot subtract {self.currency} - {other.currency}"]
            )
        result = self.amount - other.amount
        if result < 0:
            raise DomainValidationError(
                [f"Money subtraction would go negative: {self.amount} - {other.amount}"]
            )
        return Money(result, self.currency)

    def __mul__(self, factor: float) -> Money:
        if factor < 0:
            raise DomainValidationError(
                [f"Cannot multiply Money by negative factor: {factor}"]
            )
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
            raise DomainValidationError(
                [f"Price must be positive and finite: {self.value}"]
            )


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
        errors = []
        if not -1.0 <= self.score <= 1.0:
            errors.append(f"CompositeSignal score must be in [-1, 1]: {self.score}")
        if self.timescale not in self.VALID_TIMESCALES:
            errors.append(f"Invalid timescale: {self.timescale}")
        if errors:
            raise DomainValidationError(errors)

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
        errors = []
        if self.price <= 0:
            errors.append(f"StopLevel price must be positive: {self.price}")
        if self.is_trailing and self.trail_pct <= 0:
            errors.append(f"Trailing stop needs positive trail_pct: {self.trail_pct}")
        if errors:
            raise DomainValidationError(errors)


@dataclass(frozen=True)
class ProbabilitySignal:
    """
    Calibrated probability forecast from a trained LightGBM head.

    Sourced from the TimesFM service's /v2/probability/15m endpoint. Unlike
    CompositeSignal (a heuristic blend of indicators in [-1, 1]),
    ProbabilitySignal is the output of a classifier trained on Polymarket
    UpDown window outcomes: it predicts the probability that the current
    window will close ABOVE its open price.

    The crucial difference from CompositeSignal: this is directional and
    calibrated. At probability_up > 0.70 the model was correct 77.5% of the
    time in the 5-day backtest (vs 50% random baseline). At probability_up
    > 0.75 the hit rate is 96.87% and the trade is profitable even after
    round-trip Binance taker fees.

    seconds_to_close is the horizon of this prediction: the model head is
    trained to forecast the window close that many seconds in the future.
    The position should be exited at (or before) window close, NOT on
    signal reversal — the whole point of the calibration is that the
    probability already accounts for interim noise.

    window_open_ts and window_close_ts are the unix timestamps of the
    prediction window boundaries, so the caller can avoid double-trading
    the same window.
    """

    probability_up: float  # [0, 1], calibrated
    asset: str
    timescale: str  # "15m" for now — the only trained horizon
    seconds_to_close: int  # seconds until the prediction target
    window_open_ts: int  # unix seconds, window boundary
    window_close_ts: int  # unix seconds, window boundary
    model_version: str  # for audit trail in margin_positions
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        errors = []
        if not 0.0 <= self.probability_up <= 1.0:
            errors.append(
                f"ProbabilitySignal probability_up must be in [0, 1]: "
                f"{self.probability_up}"
            )
        if self.seconds_to_close < 0:
            errors.append(
                f"seconds_to_close cannot be negative: {self.seconds_to_close}"
            )
        if errors:
            raise DomainValidationError(errors)

    @property
    def probability_down(self) -> float:
        return 1.0 - self.probability_up

    @property
    def conviction(self) -> float:
        """Distance from 0.5 — how far from random the model thinks this is."""
        return abs(self.probability_up - 0.5)

    @property
    def suggested_side(self) -> TradeSide:
        return TradeSide.LONG if self.probability_up > 0.5 else TradeSide.SHORT

    def meets_threshold(self, min_conviction: float) -> bool:
        """True if |p_up - 0.5| >= min_conviction (e.g., 0.20 → p>0.70 or p<0.30)."""
        return self.conviction >= min_conviction
