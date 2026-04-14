"""
Pydantic schemas for status API responses.

These schemas define the structure of the HTTP API responses
for the status server endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionState(Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class PositionDict:
    """Serialized position representation for JSON response."""
    id: str
    asset: str
    side: str  # PositionSide.value
    state: str  # PositionState.value
    entry_price: Optional[float]
    notional: Optional[float]
    collateral: Optional[float]
    entry_signal_score: Optional[float]
    entry_timescale: Optional[str]
    unrealised_pnl: float
    realised_pnl: float
    exit_reason: Optional[str]
    opened_at: str
    closed_at: Optional[str]
    hold_duration_s: Optional[float]


@dataclass
class PortfolioDict:
    """Serialized portfolio representation for JSON response."""
    balance: float
    exposure: float
    leverage: float
    is_active: bool
    kill_switch: bool
    paper_mode: bool
    daily_pnl: float
    consecutive_losses: int


@dataclass
class StatsDict:
    """Statistics summary for JSON response."""
    open_count: int
    closed_count: int
    total_realised_pnl: float
    win_rate: float


@dataclass
class ExecutionDict:
    """Execution context for JSON response."""
    venue: str
    paper_mode: bool
    fees: Optional[dict] = None
    price_feed_healthy: Optional[bool] = None
    strategy: Optional[str] = None
    error: Optional[str] = None


@dataclass
class StatusResponse:
    """Complete /status endpoint response."""
    portfolio: PortfolioDict
    positions: list[PositionDict]
    stats: StatsDict
    execution: ExecutionDict


@dataclass
class HealthResponse:
    """/health endpoint response."""
    status: str = "ok"


@dataclass
class LogEntry:
    """Individual log entry."""
    timestamp: str
    level: str
    message: str
    module: Optional[str] = None


@dataclass
class LogsResponse:
    """/logs endpoint response."""
    logs: list[LogEntry]
    count: int


@dataclass
class HistoryRow:
    """Individual history row (closed position)."""
    id: str
    asset: str
    side: str
    entry_price: float
    exit_price: Optional[float]
    notional: float
    collateral: float
    realised_pnl: float
    exit_reason: str
    opened_at: str
    closed_at: str
    hold_duration_s: float
    entry_signal_score: Optional[float]
    entry_timescale: Optional[str]


@dataclass
class HistoryResponse:
    """/history endpoint response."""
    rows: list[HistoryRow]
    total: int
    limit: int
    offset: int
