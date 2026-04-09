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
    Money,
    Price,
    TradeSide,
)


class ExchangePort(abc.ABC):
    """Interface for exchange operations (Binance margin or paper)."""

    @abc.abstractmethod
    async def place_market_order(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> tuple[str, Price]:
        """
        Place a market order. Returns (order_id, fill_price).
        Raises ExchangeError on failure.
        """
        ...

    @abc.abstractmethod
    async def close_position(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> tuple[str, Price]:
        """
        Close a position with a market order in the opposite direction.
        Returns (order_id, fill_price).
        """
        ...

    @abc.abstractmethod
    async def get_balance(self) -> Money:
        """Get available margin balance."""
        ...

    @abc.abstractmethod
    async def get_current_price(self, symbol: str) -> Price:
        """Get current market price."""
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
