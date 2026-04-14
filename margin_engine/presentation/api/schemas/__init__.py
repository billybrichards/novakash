"""
Request/response schemas for the presentation layer API.

All schemas are dataclass-based for simplicity and
type safety without external dependencies.
"""
from margin_engine.presentation.api.schemas.status_schemas import (
    PositionSide,
    PositionState,
    PositionDict,
    PortfolioDict,
    StatsDict,
    ExecutionDict,
    StatusResponse,
    HealthResponse,
    LogEntry,
    LogsResponse,
    HistoryRow,
    HistoryResponse,
)

__all__ = [
    "PositionSide",
    "PositionState",
    "PositionDict",
    "PortfolioDict",
    "StatsDict",
    "ExecutionDict",
    "StatusResponse",
    "HealthResponse",
    "LogEntry",
    "LogsResponse",
    "HistoryRow",
    "HistoryResponse",
]
