"""Tests for BackfillRealizedOutcomeUseCase and P&L helper functions.

Uses AsyncMock repo. Verifies:
  - WIN / LOSS / PUSH pnl computations
  - repo.backfill_outcome called with correct values
  - exceptions from repo are swallowed
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from exit_monitor.use_cases.backfill_realized_outcome import (
    BackfillRealizedOutcomeUseCase,
    _pnl_held_to_close,
    _pnl_shadow_exit,
)


# ── P&L helper functions ─────────────────────────────────────────────────────


class TestPnlHeldToClose:
    def test_win_pnl(self):
        """WIN at $0.85 fill → net = 1.00 - 0.85 = 0.15."""
        assert abs(_pnl_held_to_close("WIN", 0.85) - 0.15) < 1e-5

    def test_loss_pnl(self):
        """LOSS at $0.85 fill → net = -0.85."""
        assert abs(_pnl_held_to_close("LOSS", 0.85) - (-0.85)) < 1e-5

    def test_push_pnl(self):
        """PUSH → 0.0."""
        assert _pnl_held_to_close("PUSH", 0.85) == 0.0


class TestPnlShadowExit:
    def test_with_clob_bid(self):
        """Shadow exit pnl = clob_bid - fill_price."""
        result = _pnl_shadow_exit(fill_price=0.85, clob_bid=0.01)
        assert abs(result - (0.01 - 0.85)) < 1e-5

    def test_without_clob_bid(self):
        """Returns None when clob_bid is None (no CLOB data available)."""
        assert _pnl_shadow_exit(fill_price=0.85, clob_bid=None) is None


# ── Use case ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.backfill_outcome = AsyncMock(return_value=3)
    return repo


@pytest.mark.asyncio
async def test_execute_win(mock_repo):
    """WIN outcome → backfill_outcome called with positive pnl."""
    uc = BackfillRealizedOutcomeUseCase(repo=mock_repo)
    await uc.execute(decision_id=42, outcome="WIN", fill_price=0.85)

    mock_repo.backfill_outcome.assert_awaited_once()
    kwargs = mock_repo.backfill_outcome.call_args[1]
    assert kwargs["decision_id"] == 42
    assert kwargs["realized_outcome"] == "WIN"
    assert abs(kwargs["realized_pnl_held_to_close"] - 0.15) < 1e-5


@pytest.mark.asyncio
async def test_execute_loss(mock_repo):
    """LOSS outcome → backfill_outcome called with negative pnl."""
    uc = BackfillRealizedOutcomeUseCase(repo=mock_repo)
    await uc.execute(decision_id=7, outcome="LOSS", fill_price=0.85)

    kwargs = mock_repo.backfill_outcome.call_args[1]
    assert kwargs["realized_outcome"] == "LOSS"
    assert abs(kwargs["realized_pnl_held_to_close"] - (-0.85)) < 1e-5


@pytest.mark.asyncio
async def test_execute_repo_exception_swallowed(mock_repo):
    """Exception from repo is swallowed — caller never sees it."""
    mock_repo.backfill_outcome = AsyncMock(side_effect=RuntimeError("db error"))
    uc = BackfillRealizedOutcomeUseCase(repo=mock_repo)
    # Should not raise
    await uc.execute(decision_id=1, outcome="WIN", fill_price=0.90)
