"""Port (interface) fakes for use-case tests.

Pattern from clean-arch guide §Testing Strategy: "Use cases with mocks"
means mocking ports, not concrete infrastructure. Each fake implements
the minimal interface required to satisfy the port contract.
"""
from __future__ import annotations

from unittest.mock import AsyncMock


def fake_trade_repository() -> AsyncMock:
    """Return an AsyncMock conforming to ITradeRepository."""
    mock = AsyncMock()
    mock.save.return_value = None
    mock.find_by_id.return_value = None
    mock.find_unresolved_paper_trades.return_value = []
    mock.fetch_trades.return_value = []
    mock.manual_trades_joined_poly_fills.return_value = []
    return mock


def fake_window_repository() -> AsyncMock:
    """Return an AsyncMock conforming to IWindowRepository."""
    mock = AsyncMock()
    mock.get_actual_direction.return_value = None
    mock.save_window_state.return_value = None
    return mock


def fake_risk_manager(*, paper_mode: bool = True) -> AsyncMock:
    """Return a fake RiskManager that approves by default."""
    mock = AsyncMock()
    mock.approve_bet.return_value = True
    mock.get_status.return_value = {
        "paper_mode": paper_mode,
        "bankroll": 500.0,
        "drawdown": 0.0,
        "kill_switch_active": False,
    }
    return mock


def fake_alerts_gateway() -> AsyncMock:
    """Return a fake telegram-alerts gateway."""
    mock = AsyncMock()
    mock.send_system_alert.return_value = True
    mock.send_trade_alert.return_value = True
    return mock


def fake_execution_guard() -> AsyncMock:
    """Return a fake PgWindowExecutionGuard that permits all executions."""
    mock = AsyncMock()
    mock.try_claim.return_value = True
    mock.is_claimed.return_value = False
    return mock
