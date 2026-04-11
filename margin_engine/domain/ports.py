"""
Domain ports — interfaces that the domain declares and outer layers implement.

These are the dependency inversion boundaries. The domain layer never imports
from adapters or infrastructure; instead, it depends on these abstract ports.
Adapters implement them.
"""
from __future__ import annotations

import abc
from typing import Optional

from margin_engine.domain.entities.position import Position
from margin_engine.domain.value_objects import (
    CompositeSignal,
    FillResult,
    Money,
    Price,
    ProbabilitySignal,
    TradeSide,
    V4Snapshot,
)


class ExchangePort(abc.ABC):
    """Interface for exchange operations (Binance margin or paper)."""

    @abc.abstractmethod
    async def place_market_order(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> FillResult:
        """
        Place a market order. Returns a FillResult with exchange ground truth:
        actual filled notional, real commission, etc.
        Raises ExchangeError on failure.
        """
        ...

    @abc.abstractmethod
    async def close_position(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> FillResult:
        """
        Close a position with a market order in the opposite direction.
        Returns a FillResult with exchange ground truth. The `side` argument
        is the side of the position BEING CLOSED — the adapter flips it to
        determine the order direction.
        """
        ...

    @abc.abstractmethod
    async def get_balance(self) -> Money:
        """Get available margin balance."""
        ...

    @abc.abstractmethod
    async def get_current_price(self, symbol: str) -> Price:
        """
        Get a reference mid/last price. Prefer get_mark() for stop-loss and
        take-profit evaluation — this is a convenience for cases that just
        want "a recent price" without caring about bid/ask side.
        """
        ...

    @abc.abstractmethod
    async def get_mark(self, symbol: str, side: TradeSide) -> Price:
        """
        Return the price at which a position of the given side would CLOSE.
        For LONG positions: returns the bid (you'd sell into it).
        For SHORT positions: returns the ask (you'd buy from it).

        This is the price stop-loss and take-profit evaluation should compare
        against — NOT the last-trade ticker, which can be stale or reflect
        a print on the opposite side of the book. The difference matters
        during fast moves or thin books.
        """
        ...

    @abc.abstractmethod
    async def get_unrealised_pnl(self, position: Position) -> float:
        """
        Net unrealised P&L if the position closed right now, inclusive of:
          - entry commission (already paid)
          - estimated exit commission at current fee rate
          - accrued borrow interest over the hold duration

        Returns signed float (positive = in profit), denominated in USDT.

        Live mode queries Binance bookTicker for the mark and uses stored
        actual entry commission. Paper mode uses its modeled spread.
        Per-position interest is still an estimate in both modes because
        Binance cross-margin doesn't report interest per position.
        """
        ...


class SignalPort(abc.ABC):
    """Interface for receiving composite signals from the v3 system."""

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish connection to signal source."""
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from signal source."""
        ...

    @abc.abstractmethod
    async def get_latest_signal(self, timescale: str) -> Optional[CompositeSignal]:
        """Get the latest composite signal for a timescale."""
        ...

    @abc.abstractmethod
    async def get_all_signals(self) -> dict[str, CompositeSignal]:
        """Get latest signals for all timescales."""
        ...


class ProbabilityPort(abc.ABC):
    """
    Interface for calibrated probability forecasts.

    Distinct from SignalPort (v3 composite) because it's a fundamentally
    different class of prediction: a trained classifier's P(window closes
    up) rather than a heuristic blend. Kept as a separate port so the
    composite can continue to serve its role as a regime/volatility filter
    while directional conviction comes from the ML model.
    """

    @abc.abstractmethod
    async def connect(self) -> None:
        """Start the background poller."""
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Stop the background poller."""
        ...

    @abc.abstractmethod
    async def get_latest(
        self,
        asset: str = "BTC",
        timescale: str = "15m",
    ) -> Optional[ProbabilitySignal]:
        """
        Most recent probability forecast, or None if stale/unavailable.

        Returns None if the cached prediction is older than the freshness
        window (default 120s) — better to skip a tick than trade on stale
        ML output.
        """
        ...

    @abc.abstractmethod
    async def force_refresh(
        self,
        asset: str = "BTC",
        timescale: str = "15m",
    ) -> Optional[ProbabilitySignal]:
        """
        Bypass the standard poll cadence: fire an immediate HTTP call,
        refresh the cache, and return the fresh value.

        Used by the v2-fallback continuation path in ManagePositionsUseCase.
        The standard 30s polling cadence is too coarse for continuation
        checks at window close — a cached reading from t-27s describes
        the PREVIOUS window, not the new one we'd be extending into.

        Returns None on any failure (network error, non-200, JSON parse).
        Never raises — callers should treat None as "no fresh data, exit
        safely" rather than propagating exceptions.
        """
        ...


class V4SnapshotPort(abc.ABC):
    """
    Interface for reading /v4/snapshot from the timesfm service.

    The v4 surface fuses per-timescale probability, quantile distribution,
    regime classification, 6-source price consensus, Claude-generated macro
    bias, upcoming macro events, cascade FSM state, and cross-timescale
    alignment into ONE atomic read. The margin engine consumes this instead
    of (or alongside) the legacy /v2/probability scalar.

    Implementations MUST fail soft: on any HTTP error, timeout, or parsing
    failure, get_latest() returns None. Callers should treat None as "no
    fresh v4 data available for this tick" and either fall back to the
    legacy ProbabilityPort path or skip the tick — never raise or stall.

    Lifecycle mirrors ProbabilityPort: connect() starts the background
    poller, disconnect() stops it cleanly. get_latest() reads from the
    in-memory cache populated by the poller.
    """

    @abc.abstractmethod
    async def connect(self) -> None:
        """Start the background poll loop."""
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Stop the background poll loop and release network resources."""
        ...

    @abc.abstractmethod
    async def get_latest(
        self,
        asset: str = "BTC",
        timescales: Optional[list[str]] = None,
    ) -> Optional[V4Snapshot]:
        """
        Return the most recent cached V4Snapshot if fresh, else None.

        Returns None when:
          - No snapshot has been received yet
          - The cached snapshot is older than the freshness window (default 10s)
          - The cached snapshot's asset doesn't match the request

        The `timescales` parameter is informational — current implementations
        always return the full set of timescales the poller was configured
        for, regardless of this argument. It exists for future variations
        where an adapter might request a narrower subset.
        """
        ...


class AlertPort(abc.ABC):
    """Interface for sending alerts (Telegram, etc.)."""

    @abc.abstractmethod
    async def send_trade_opened(self, position: Position) -> None:
        ...

    @abc.abstractmethod
    async def send_trade_closed(self, position: Position) -> None:
        ...

    @abc.abstractmethod
    async def send_kill_switch(self, reason: str) -> None:
        ...

    @abc.abstractmethod
    async def send_error(self, message: str) -> None:
        ...


class PositionRepository(abc.ABC):
    """Interface for persisting positions."""

    @abc.abstractmethod
    async def save(self, position: Position) -> None:
        ...

    @abc.abstractmethod
    async def get_open_positions(self) -> list[Position]:
        ...

    @abc.abstractmethod
    async def get_by_id(self, position_id: str) -> Optional[Position]:
        ...

    @abc.abstractmethod
    async def get_closed_today(self) -> list[Position]:
        ...


class ClockPort(abc.ABC):
    """Interface for time — allows deterministic testing."""

    @abc.abstractmethod
    def now(self) -> float:
        """Current time as Unix timestamp."""
        ...
