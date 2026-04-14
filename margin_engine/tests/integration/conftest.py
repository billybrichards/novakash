"""
Integration test fixtures for margin engine use cases.

Provides in-memory adapters and test data for testing full use case
orchestration without external dependencies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from margin_engine.domain.entities.portfolio import Portfolio
from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import (
    AlertPort,
    ExchangePort,
    FillResult,
    PositionRepository,
    ProbabilityPort,
    SignalPort,
    V4SnapshotPort,
)
from margin_engine.domain.value_objects import (
    Consensus,
    MacroBias,
    Money,
    Price,
    ProbabilitySignal,
    Quantiles,
    StopLevel,
    TimescalePayload,
    TradeSide,
    V4Snapshot,
)


# ──────────────────────────────────────────────────────────────────────────
# In-memory repository for testing persistence
# ──────────────────────────────────────────────────────────────────────────


class InMemoryPositionRepository:
    """In-memory position repository for integration tests."""

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    async def save(self, position: Position) -> None:
        """Save a position to in-memory store."""
        self._positions[position.id] = position

    async def get_open_positions(self) -> list[Position]:
        """Get all open positions."""
        return [p for p in self._positions.values() if (p.state.value if hasattr(p.state, "value") else p.state) == "OPEN"]

    async def get_by_id(self, position_id: str) -> Optional[Position]:
        """Get position by ID."""
        return self._positions.get(position_id)

    async def get_closed_today(self) -> list[Position]:
        """Get all closed positions."""
        return [p for p in self._positions.values() if (p.state.value if hasattr(p.state, "value") else p.state) == "CLOSED"]

    def clear(self) -> None:
        """Clear all positions."""
        self._positions.clear()

    def get_all(self) -> list[Position]:
        """Get all positions (open and closed)."""
        return list(self._positions.values())


# ──────────────────────────────────────────────────────────────────────────
# Mock exchange that records orders
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class RecordedOrder:
    """Record of a market order."""

    symbol: str
    side: TradeSide
    notional: Money
    fill_result: FillResult


class MockExchange:
    """Mock exchange that records orders and simulates fills."""

    def __init__(self, initial_balance: float = 500.0) -> None:
        self._balance = Money.usd(initial_balance)
        self._orders: list[RecordedOrder] = []
        self._current_price = 70000.0
        self._spread = 10.0  # bid-ask spread in USD

    async def place_market_order(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> FillResult:
        """Place a market order."""
        fill_price = (
            self._current_price + self._spread / 2
            if side == TradeSide.LONG
            else self._current_price - self._spread / 2
        )
        # Handle both Money and float
        if hasattr(notional, 'amount'):
            filled_notional = notional.amount
        else:
            filled_notional = notional
        commission = filled_notional * 0.00045  # 0.045% fee

        result = FillResult(
            order_id=f"order-{len(self._orders) + 1}",
            fill_price=Price(fill_price),
            filled_notional=filled_notional,
            commission=commission,
            commission_asset="USDT",
            commission_is_actual=True,
        )

        self._orders.append(
            RecordedOrder(
                symbol=symbol,
                side=side,
                notional=notional,
                fill_result=result,
            )
        )

        return result

    async def close_position(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> FillResult:
        """Close a position."""
        # Reverse the spread for closing
        fill_price = (
            self._current_price - self._spread / 2
            if side == TradeSide.LONG
            else self._current_price + self._spread / 2
        )
        # Handle both Money and float
        if hasattr(notional, 'amount'):
            filled_notional = notional.amount
        else:
            filled_notional = notional
        commission = filled_notional * 0.00045

        result = FillResult(
            order_id=f"close-{len(self._orders) + 1}",
            fill_price=Price(fill_price),
            filled_notional=filled_notional,
            commission=commission,
            commission_asset="USDT",
            commission_is_actual=True,
        )

        self._orders.append(
            RecordedOrder(
                symbol=symbol,
                side=side,
                notional=notional,
                fill_result=result,
            )
        )

        return result

    async def get_balance(self) -> Money:
        """Get available balance."""
        return self._balance

    async def get_current_price(self, symbol: str) -> Price:
        """Get current price."""
        return Price(self._current_price)

    async def get_mark(self, symbol: str, side: TradeSide) -> Price:
        """Get mark price for closing."""
        mark = (
            self._current_price - self._spread / 2
            if side == TradeSide.LONG
            else self._current_price + self._spread / 2
        )
        return Price(mark)

    async def get_unrealised_pnl(self, position: Position) -> float:
        """Calculate unrealised PnL."""
        mark_price = await self.get_mark(f"{position.asset}USDT", position.side)
        if position.side == TradeSide.LONG:
            pnl = (mark_price.value - position.entry_price.value) * position.size
        else:
            pnl = (position.entry_price.value - mark_price.value) * position.size
        return pnl - position.entry_commission

    def set_price(self, price: float) -> None:
        """Set current price for testing."""
        self._current_price = price

    @property
    def orders(self) -> list[RecordedOrder]:
        """Get all recorded orders."""
        return self._orders


# ──────────────────────────────────────────────────────────────────────────
# Mock alert port
# ──────────────────────────────────────────────────────────────────────────


class MockAlertPort:
    """Mock alert port that records alerts."""

    def __init__(self) -> None:
        self._alerts: list[str] = []

    async def send_trade_opened(self, position: Position) -> None:
        """Record trade opened alert."""
        self._alerts.append(f"opened:{position.id}")

    async def send_trade_closed(self, position: Position) -> None:
        """Record trade closed alert."""
        self._alerts.append(f"closed:{position.id}")

    async def send_kill_switch(self, reason: str) -> None:
        """Record kill switch alert."""
        self._alerts.append(f"kill:{reason}")

    async def send_error(self, message: str) -> None:
        """Record error alert."""
        self._alerts.append(f"error:{message}")

    @property
    def alerts(self) -> list[str]:
        """Get all recorded alerts."""
        return self._alerts

    def clear(self) -> None:
        """Clear all alerts."""
        self._alerts.clear()


# ──────────────────────────────────────────────────────────────────────────
# Mock signal and probability ports
# ──────────────────────────────────────────────────────────────────────────


class MockSignalPort:
    """Mock signal port."""

    def __init__(self) -> None:
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def get_latest_signal(self, timescale: str) -> Any:
        return None

    async def get_all_signals(self) -> dict[str, Any]:
        return {}


class MockProbabilityPort:
    """Mock probability port."""

    def __init__(self) -> None:
        self._connected = False
        self._probability: Optional[ProbabilitySignal] = None

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def get_latest(
        self,
        asset: str = "BTC",
        timescale: str = "15m",
    ) -> Optional[ProbabilitySignal]:
        return self._probability

    async def force_refresh(
        self,
        asset: str = "BTC",
        timescale: str = "15m",
    ) -> Optional[ProbabilitySignal]:
        return self._probability

    def set_probability(self, prob: Optional[ProbabilitySignal]) -> None:
        self._probability = prob


class MockV4SnapshotPort:
    """Mock V4 snapshot port."""

    def __init__(self) -> None:
        self._connected = False
        self._snapshot: Optional[V4Snapshot] = None

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def get_latest(
        self,
        asset: str = "BTC",
        timescales: Optional[list[str]] = None,
    ) -> Optional[V4Snapshot]:
        return self._snapshot

    def set_snapshot(self, snapshot: Optional[V4Snapshot]) -> None:
        self._snapshot = snapshot


# ──────────────────────────────────────────────────────────────────────────
# Test fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def in_memory_repo() -> InMemoryPositionRepository:
    """Setup in-memory position repository."""
    return InMemoryPositionRepository()


@pytest.fixture
def mock_exchange() -> MockExchange:
    """Create mock exchange that records orders."""
    return MockExchange(initial_balance=500.0)


@pytest.fixture
def mock_alerts() -> MockAlertPort:
    """Create mock alert port."""
    return MockAlertPort()


@pytest.fixture
def mock_signal_port() -> MockSignalPort:
    """Create mock signal port."""
    return MockSignalPort()


@pytest.fixture
def mock_probability_port() -> MockProbabilityPort:
    """Create mock probability port."""
    return MockProbabilityPort()


@pytest.fixture
def mock_v4_port() -> MockV4SnapshotPort:
    """Create mock V4 snapshot port."""
    return MockV4SnapshotPort()


@pytest.fixture
def test_portfolio() -> Portfolio:
    """Create test portfolio with known state."""
    portfolio = Portfolio(
        starting_capital=Money.usd(500.0),
        leverage=3,
    )
    return portfolio


@pytest.fixture
def test_v4_snapshot() -> V4Snapshot:
    """Create test v4 snapshot with known values."""
    payload = TimescalePayload(
        timescale="15m",
        status="ok",
        probability_up=0.72,
        regime="TRENDING_UP",
        expected_move_bps=20.0,
        window_close_ts=1776400000,
        quantiles_at_close=Quantiles(
            p10=69500.0,
            p25=69700.0,
            p50=70200.0,
            p75=70600.0,
            p90=71000.0,
        ),
    )

    return V4Snapshot(
        asset="BTC",
        ts=1776400000.0,
        last_price=70000.0,
        consensus=Consensus(
            safe_to_trade=True,
            safe_to_trade_reason="ok",
            reference_price=70000.0,
            max_divergence_bps=0.5,
            source_agreement_score=0.98,
        ),
        macro=MacroBias(
            bias="NEUTRAL",
            confidence=50,
            direction_gate="ALLOW_ALL",
            size_modifier=1.0,
            status="ok",
        ),
        timescales={"15m": payload},
    )


@pytest.fixture
def test_position() -> Position:
    """Create a test position."""
    position = Position(
        asset="BTC",
        side=TradeSide.LONG,
        entry_price=Price(70000.0),
        notional=Money.usd(70.0),  # ~70 USDT notional at 3x leverage
        stop_loss=StopLevel(69500.0),
        take_profit=StopLevel(70500.0),
        max_hold_seconds=900,  # 15 minutes
    )
    return position


@pytest.fixture
def setup_full_environment(
    in_memory_repo: InMemoryPositionRepository,
    mock_exchange: MockExchange,
    mock_alerts: MockAlertPort,
    mock_signal_port: MockSignalPort,
    mock_probability_port: MockProbabilityPort,
    mock_v4_port: MockV4SnapshotPort,
    test_portfolio: Portfolio,
) -> dict[str, Any]:
    """Setup complete test environment with all dependencies."""
    return {
        "repository": in_memory_repo,
        "exchange": mock_exchange,
        "alerts": mock_alerts,
        "signal_port": mock_signal_port,
        "probability_port": mock_probability_port,
        "v4_port": mock_v4_port,
        "portfolio": test_portfolio,
    }
