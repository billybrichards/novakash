"""Tests for ReconcilePositionsUseCase — paper + live resolution."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from engine.domain.value_objects import PositionOutcome, ResolutionResult, WindowKey
from engine.use_cases.reconcile_positions import ReconcilePositionsUseCase


def _pos(
    condition_id="cond-abc", token_id="tok-123456789012345",
    outcome="WIN", size=10.0, avg_price=0.50, cost=5.0,
    value=10.0, pnl_raw=5.0,
):
    return PositionOutcome(
        condition_id=condition_id, token_id=token_id, outcome=outcome,
        size=size, avg_price=avg_price, cost=cost, value=value,
        pnl_raw=pnl_raw,
    )


def _match(
    trade_id="trade-001", token_id="tok-123456789012345",
    stake_usd=5.0, entry_price=0.50, entry_reason="VPIN gate pass",
    asset="BTC", window_ts=1700000000,
):
    return {
        "id": trade_id, "token_id": token_id, "stake_usd": stake_usd,
        "entry_price": entry_price, "entry_reason": entry_reason,
        "asset": asset, "window_ts": window_ts,
    }


class Ports:
    def __init__(self):
        self.trade_repo = AsyncMock()
        self.window_state = AsyncMock()
        self.alerts = AsyncMock()
        self.clock = MagicMock()
        self.clock.now.return_value = 1700000100.0

    def uc(self):
        return ReconcilePositionsUseCase(
            trade_repo=self.trade_repo,
            window_state=self.window_state,
            alerts=self.alerts,
            clock=self.clock,
        )


@pytest.mark.asyncio
async def test_exact_match_win():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = _match()

    result = await p.uc().resolve_one(_pos(outcome="WIN"))

    assert result is not None
    assert result.outcome == "WIN"
    assert result.status == "RESOLVED_WIN"
    assert result.matched_trade_id == "trade-001"
    assert result.match_method == "exact"
    assert result.pnl_usd == 5.0  # shares(10) - stake(5) = 5

    p.trade_repo.resolve_trade.assert_called_once_with(
        trade_id="trade-001", outcome="WIN", pnl_usd=5.0, status="RESOLVED_WIN",
    )


@pytest.mark.asyncio
async def test_exact_match_loss():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = _match()

    result = await p.uc().resolve_one(_pos(outcome="LOSS"))

    assert result.outcome == "LOSS"
    assert result.status == "RESOLVED_LOSS"
    assert result.pnl_usd == -5.0


@pytest.mark.asyncio
async def test_prefix_match_fallback():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = None
    p.trade_repo.find_by_token_prefix.return_value = _match()

    result = await p.uc().resolve_one(_pos(token_id="tok-123456789012345-extra"))

    assert result is not None
    assert result.match_method == "prefix"


@pytest.mark.asyncio
async def test_cost_fallback_match():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = None
    p.trade_repo.find_by_token_prefix.return_value = None
    p.trade_repo.find_by_approximate_cost.return_value = _match(trade_id="cost-001")

    result = await p.uc().resolve_one(_pos(token_id="unknown-token-id", cost=4.80))

    assert result is not None
    assert result.match_method == "cost_fallback"
    assert result.matched_trade_id == "cost-001"


@pytest.mark.asyncio
async def test_no_match_returns_none():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = None
    p.trade_repo.find_by_token_prefix.return_value = None
    p.trade_repo.find_by_approximate_cost.return_value = None

    result = await p.uc().resolve_one(_pos())

    assert result is None
    p.trade_repo.resolve_trade.assert_not_called()
    p.alerts.send_system_alert.assert_called_once()


@pytest.mark.asyncio
async def test_window_state_mark_resolved_called():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = _match(asset="BTC", window_ts=1700000000)

    await p.uc().resolve_one(_pos(outcome="WIN"))

    p.window_state.mark_resolved.assert_called_once()
    wk = p.window_state.mark_resolved.call_args.args[0]
    assert wk.asset == "BTC"
    assert wk.window_ts == 1700000000


@pytest.mark.asyncio
async def test_pnl_uses_per_trade_data_not_aggregate():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = _match(stake_usd=5.0, entry_price=0.50)

    result = await p.uc().resolve_one(_pos(outcome="WIN", cost=20.0, size=40.0))

    # per-trade: shares=5/0.5=10, pnl=10-5=5, NOT aggregate 40-20=20
    assert result.pnl_usd == 5.0


@pytest.mark.asyncio
async def test_short_token_id_skips_prefix_match():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = None
    p.trade_repo.find_by_approximate_cost.return_value = None

    result = await p.uc().resolve_one(_pos(token_id="short"))

    assert result is None
    p.trade_repo.find_by_token_prefix.assert_not_called()


@pytest.mark.asyncio
async def test_alert_failure_does_not_break_resolution():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = _match()
    p.alerts.send_system_alert.side_effect = RuntimeError("Telegram down")

    result = await p.uc().resolve_one(_pos(outcome="WIN"))

    assert result is not None
    assert result.outcome == "WIN"


@pytest.mark.asyncio
async def test_mark_resolved_failure_non_fatal():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = _match()
    p.window_state.mark_resolved.side_effect = RuntimeError("DB timeout")

    result = await p.uc().resolve_one(_pos(outcome="LOSS"))

    assert result is not None
    assert result.outcome == "LOSS"
    p.trade_repo.resolve_trade.assert_called_once()


@pytest.mark.asyncio
async def test_empty_token_id_skips_token_matching():
    p = Ports()
    p.trade_repo.find_by_approximate_cost.return_value = _match()

    result = await p.uc().resolve_one(_pos(token_id="", cost=5.0))

    assert result is not None
    assert result.match_method == "cost_fallback"
    p.trade_repo.find_by_token_id.assert_not_called()
    p.trade_repo.find_by_token_prefix.assert_not_called()


# ---------------------------------------------------------------------------
# PgWindowRepository.get_actual_direction
# ---------------------------------------------------------------------------

class TestGetActualDirection:
    """Unit tests via a mock pool — verifies SQL and return value."""

    def _make_repo_with_pool(self, fetchrow_result):
        import asyncpg
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=fetchrow_result)
        pool = AsyncMock()
        pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=conn),
                __aexit__=AsyncMock(return_value=False),
            )
        )
        from engine.adapters.persistence.pg_window_repo import PgWindowRepository
        repo = PgWindowRepository(pool=pool)
        return repo, conn

    @pytest.mark.asyncio
    async def test_returns_direction_when_row_exists(self):
        row = {"actual_direction": "UP"}
        repo, conn = self._make_repo_with_pool(row)
        key = WindowKey(asset="BTC", window_ts=1776109200)
        result = await repo.get_actual_direction(key)
        assert result == "UP"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_row(self):
        repo, conn = self._make_repo_with_pool(None)
        key = WindowKey(asset="BTC", window_ts=1776109200)
        result = await repo.get_actual_direction(key)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_pool_is_none(self):
        from engine.adapters.persistence.pg_window_repo import PgWindowRepository
        repo = PgWindowRepository(pool=None)
        key = WindowKey(asset="BTC", window_ts=1776109200)
        result = await repo.get_actual_direction(key)
        assert result is None


# ---------------------------------------------------------------------------
# PgTradeRepository.find_unresolved_paper_trades
# ---------------------------------------------------------------------------


class TestFindUnresolvedPaperTrades:

    def _make_repo_with_pool(self, fetch_result):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=fetch_result)
        pool = AsyncMock()
        pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=conn),
                __aexit__=AsyncMock(return_value=False),
            )
        )
        from engine.adapters.persistence.pg_trade_repo import PgTradeRepository
        repo = PgTradeRepository(pool=pool)
        return repo, conn

    @pytest.mark.asyncio
    async def test_returns_rows_as_dicts(self):
        fake_row = {
            "id": "abc123", "order_id": "5min-1234", "direction": "UP",
            "stake_usd": 10.0, "entry_price": 0.65,
            "execution_mode": "paper", "metadata": '{"window_ts": "1776109200"}',
            "asset": "BTC", "window_ts": "1776109200", "created_at": None,
        }

        class FakeRow(dict):
            pass

        row = FakeRow(fake_row)
        repo, conn = self._make_repo_with_pool([row])

        results = await repo.find_unresolved_paper_trades(min_age_seconds=360)
        assert len(results) == 1
        assert results[0]["id"] == "abc123"
        assert results[0]["direction"] == "UP"

    @pytest.mark.asyncio
    async def test_returns_empty_when_pool_none(self):
        from engine.adapters.persistence.pg_trade_repo import PgTradeRepository
        repo = PgTradeRepository(pool=None)
        results = await repo.find_unresolved_paper_trades()
        assert results == []
