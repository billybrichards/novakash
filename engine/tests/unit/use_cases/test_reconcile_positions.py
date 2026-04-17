"""Tests for ReconcilePositionsUseCase — paper + live resolution."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from domain.value_objects import PositionOutcome, ResolutionResult, WindowKey
from use_cases.reconcile_positions import ReconcilePositionsUseCase


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
    polymarket_order_id="0xabcdef0123456789",
    polymarket_tx_hash="0x9999" * 8,
):
    """Default fixture represents a REAL trade: non-synthetic order_id +
    on-chain tx_hash. Tests that exercise the phantom-trade guard can
    override these to empty/synthetic values to simulate the failure mode.
    """
    return {
        "id": trade_id, "token_id": token_id, "stake_usd": stake_usd,
        "entry_price": entry_price, "entry_reason": entry_reason,
        "asset": asset, "window_ts": window_ts,
        "polymarket_order_id": polymarket_order_id,
        "polymarket_tx_hash": polymarket_tx_hash,
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
    assert result.outcome == "RESOLVED_WIN"
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

    assert result.outcome == "RESOLVED_LOSS"
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


# Phantom-trade guard (incident #4881, 2026-04-17). The cost-fallback tier
# must refuse to match on-chain positions against local trades that have no
# on-chain evidence. The three tests below lock down that guard.


@pytest.mark.asyncio
async def test_cost_fallback_rejected_when_trade_is_phantom():
    """Trade with no tx_hash AND synthetic/missing order_id must NOT match."""
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = None
    p.trade_repo.find_by_token_prefix.return_value = None
    p.trade_repo.find_by_approximate_cost.return_value = _match(
        trade_id="phantom-001",
        polymarket_order_id="",           # synthetic / missing
        polymarket_tx_hash="",             # no on-chain evidence
    )

    result = await p.uc().resolve_one(
        _pos(token_id="unknown-token-id", cost=3.35)
    )

    assert result is None
    p.trade_repo.resolve_trade.assert_not_called()


@pytest.mark.asyncio
async def test_cost_fallback_rejected_on_paper_order_id_prefix():
    """Synthetic prefixes (paper-, fak-, fok-, gtc-) + null tx → reject."""
    for synthetic_prefix in ("paper-abc", "fak-xyz", "fok-123", "gtc-def"):
        p = Ports()
        p.trade_repo.find_by_token_id.return_value = None
        p.trade_repo.find_by_token_prefix.return_value = None
        p.trade_repo.find_by_approximate_cost.return_value = _match(
            trade_id=f"synth-{synthetic_prefix}",
            polymarket_order_id=synthetic_prefix,
            polymarket_tx_hash="",
        )
        result = await p.uc().resolve_one(_pos(token_id="other", cost=3.35))
        assert result is None, (
            f"Cost-fallback wrongly matched synthetic-prefix order_id "
            f"{synthetic_prefix!r} with no tx_hash"
        )


@pytest.mark.asyncio
async def test_cost_fallback_accepted_when_trade_has_real_tx_hash():
    """Trade with a real on-chain tx_hash is a legitimate cost-fallback candidate,
    even if the local order_id happens to look synthetic. tx_hash is the strong
    signal — if the chain says it's real, we trust it."""
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = None
    p.trade_repo.find_by_token_prefix.return_value = None
    p.trade_repo.find_by_approximate_cost.return_value = _match(
        trade_id="real-with-tx",
        polymarket_order_id="fak-stale",
        polymarket_tx_hash="0x" + "ab" * 32,  # real tx
    )

    result = await p.uc().resolve_one(_pos(token_id="other", cost=4.80))

    assert result is not None
    assert result.match_method == "cost_fallback"
    assert result.matched_trade_id == "real-with-tx"


@pytest.mark.asyncio
async def test_cost_fallback_accepted_when_order_id_is_real_hash():
    """Real CLOB order_id (non-synthetic prefix) implies a real trade even
    when tx_hash hasn't been backfilled yet — accept."""
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = None
    p.trade_repo.find_by_token_prefix.return_value = None
    p.trade_repo.find_by_approximate_cost.return_value = _match(
        trade_id="real-by-order-id",
        polymarket_order_id="0xd445168bf9e661" + "0" * 50,
        polymarket_tx_hash="",  # tx not backfilled yet
    )

    result = await p.uc().resolve_one(_pos(token_id="other", cost=4.80))

    assert result is not None
    assert result.match_method == "cost_fallback"


@pytest.mark.asyncio
async def test_no_match_returns_none():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = None
    p.trade_repo.find_by_token_prefix.return_value = None
    p.trade_repo.find_by_approximate_cost.return_value = None

    result = await p.uc().resolve_one(_pos())

    assert result is None
    p.trade_repo.resolve_trade.assert_not_called()
    # resolve_one() queues the alert into _pending_live_alerts (batched pattern)
    # and does NOT call send_system_alert directly — that fires only in execute()
    # via _flush_resolution_alerts. Assert not called here to guard against regression.
    p.alerts.send_system_alert.assert_not_called()


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
    assert result.outcome == "RESOLVED_WIN"


@pytest.mark.asyncio
async def test_mark_resolved_failure_non_fatal():
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = _match()
    p.window_state.mark_resolved.side_effect = RuntimeError("DB timeout")

    result = await p.uc().resolve_one(_pos(outcome="LOSS"))

    assert result is not None
    assert result.outcome == "RESOLVED_LOSS"
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
# v4.4.0 (2026-04-16): TG alert payload enrichment + matched/orphan split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matched_win_alert_carries_direction_entry_window_ts():
    """Matched resolution enriches alert payload with trade metadata."""
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = {
        **_match(stake_usd=4.86, entry_price=0.54, window_ts=1776323700),
        "direction": "UP",
        "strategy": "v4_fusion",
    }
    uc = p.uc()

    await uc.resolve_one(_pos(outcome="WIN", cost=4.86))

    assert len(uc._pending_live_alerts) == 1
    a = uc._pending_live_alerts[0]
    assert a["matched"] is True
    assert a["direction"] == "UP"
    assert a["entry_price"] == 0.54
    assert a["window_ts"] == 1776323700
    assert a["strategy"] == "v4_fusion"
    assert a["match_method"] == "exact"


@pytest.mark.asyncio
async def test_orphan_alert_has_no_direction():
    """Orphan (no DB match) records outcome but leaves direction/window None."""
    p = Ports()
    p.trade_repo.find_by_token_id.return_value = None
    p.trade_repo.find_by_token_prefix.return_value = None
    p.trade_repo.find_by_approximate_cost.return_value = None
    uc = p.uc()

    await uc.resolve_one(_pos(outcome="LOSS", cost=5.0))

    assert len(uc._pending_live_alerts) == 1
    a = uc._pending_live_alerts[0]
    assert a["matched"] is False
    assert a["direction"] is None
    assert a["window_ts"] is None
    assert a["match_method"] is None


@pytest.mark.asyncio
async def test_flush_separates_matched_from_orphans():
    """Flushed message shows MATCHED count + ORPHAN count separately."""
    p = Ports()
    uc = p.uc()

    # 1 matched WIN, 1 matched LOSS, 2 orphan LOSS
    uc._pending_live_alerts = [
        {
            "outcome": "WIN", "pnl": 4.17, "cost": 4.86,
            "entry_price": 0.54, "matched": True, "condition_id": "c1",
            "direction": "UP", "window_ts": 1776323700,
            "match_method": "exact", "strategy": "v4_fusion",
        },
        {
            "outcome": "LOSS", "pnl": -4.86, "cost": 4.86,
            "entry_price": 0.56, "matched": True, "condition_id": "c2",
            "direction": "UP", "window_ts": 1776324000,
            "match_method": "cost_fallback", "strategy": "v4_fusion",
        },
        {
            "outcome": "LOSS", "pnl": -4.86, "cost": 4.86,
            "entry_price": None, "matched": False, "condition_id": "c3",
            "direction": None, "window_ts": None,
            "match_method": None, "strategy": None,
        },
        {
            "outcome": "LOSS", "pnl": -3.50, "cost": 3.50,
            "entry_price": None, "matched": False, "condition_id": "c4",
            "direction": None, "window_ts": None,
            "match_method": None, "strategy": None,
        },
    ]

    await uc._flush_resolution_alerts()

    # Capture the message passed to alerter
    send = p.alerts.send_raw_message
    if not send.call_args:
        send = p.alerts.send_system_alert
    msg = send.call_args.args[0]

    # Matched section shows 1W/1L (not 1W/3L — orphans excluded)
    assert "matched: 1 WIN / 1 LOSS" in msg
    # Matched net is -$0.69 (not -$8.05 — orphans excluded from net)
    assert "-$0.69" in msg
    # Orphans shown separately with their own count
    assert "Orphans: 2" in msg
    assert "pre-#211 legacy" in msg
    # Per-trade detail for matched includes direction + entry price
    assert "UP" in msg
    assert "$0.540" in msg
    # Window time rendered HH:MM UTC
    assert "UTC" in msg


@pytest.mark.asyncio
async def test_flush_silent_when_nothing_pending():
    """Empty pass produces no alert."""
    p = Ports()
    uc = p.uc()
    uc._pending_live_alerts = []
    uc._pending_paper_alerts = []

    await uc._flush_resolution_alerts()

    p.alerts.send_raw_message.assert_not_called()
    p.alerts.send_system_alert.assert_not_called()


# ---------------------------------------------------------------------------
# PgWindowRepository.get_actual_direction
# ---------------------------------------------------------------------------

class TestGetActualDirection:
    """Unit tests via a mock pool — verifies SQL and return value."""

    def _make_repo_with_pool(self, fetchrow_result):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=fetchrow_result)
        pool = AsyncMock()
        pool.acquire = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=conn),
                __aexit__=AsyncMock(return_value=False),
            )
        )
        from adapters.persistence.pg_window_repo import PgWindowRepository
        repo = PgWindowRepository(pool=pool)
        return repo, conn

    @pytest.mark.asyncio
    async def test_returns_direction_when_row_exists(self):
        row = {"actual_direction": "UP"}
        repo, conn = self._make_repo_with_pool(row)
        key = WindowKey(asset="BTC", window_ts=1776109200)
        result = await repo.get_actual_direction(key)
        assert result == "UP"
        conn.fetchrow.assert_called_once()
        call_args = conn.fetchrow.call_args.args
        assert call_args[1] == 1776109200  # window_ts
        assert call_args[2] == "BTC"       # asset

    @pytest.mark.asyncio
    async def test_returns_none_when_no_row(self):
        repo, conn = self._make_repo_with_pool(None)
        key = WindowKey(asset="BTC", window_ts=1776109200)
        result = await repo.get_actual_direction(key)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_pool_is_none(self):
        from adapters.persistence.pg_window_repo import PgWindowRepository
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
        from adapters.persistence.pg_trade_repo import PgTradeRepository
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
        conn.fetch.assert_called_once()
        call_args = conn.fetch.call_args.args
        assert call_args[1] == 360  # min_age_seconds

    @pytest.mark.asyncio
    async def test_returns_empty_when_pool_none(self):
        from adapters.persistence.pg_trade_repo import PgTradeRepository
        repo = PgTradeRepository(pool=None)
        results = await repo.find_unresolved_paper_trades()
        assert results == []


# ---------------------------------------------------------------------------
# Shared helpers for TestResolvePaperBatch / TestExecute
# ---------------------------------------------------------------------------

def _make_trade_repo(paper_trades=None):
    repo = AsyncMock()
    repo.find_unresolved_paper_trades = AsyncMock(return_value=paper_trades or [])
    repo.resolve_trade = AsyncMock(return_value=None)
    # stubs for live resolution path
    repo.find_by_token_id = AsyncMock(return_value=None)
    repo.find_by_token_prefix = AsyncMock(return_value=None)
    repo.find_by_approximate_cost = AsyncMock(return_value=None)
    return repo


def _make_window_repo(actual_direction=None):
    repo = AsyncMock()
    repo.get_actual_direction = AsyncMock(return_value=actual_direction)
    repo.mark_resolved = AsyncMock(return_value=None)
    return repo


def _make_alerts():
    alerts = AsyncMock()
    alerts.send_system_alert = AsyncMock(return_value=None)
    return alerts


def _make_clock():
    clock = MagicMock()
    clock.now.return_value = 1700000100.0
    return clock


# ---------------------------------------------------------------------------
# ReconcilePositionsUseCase._resolve_paper_batch
# ---------------------------------------------------------------------------

class TestResolvePaperBatch:

    def _make_uc(self, paper_trades=None, actual_direction=None):
        from use_cases.reconcile_positions import ReconcilePositionsUseCase
        trade_repo = _make_trade_repo(paper_trades=paper_trades)
        window_repo = _make_window_repo(actual_direction=actual_direction)
        return (
            ReconcilePositionsUseCase(
                trade_repo=trade_repo,
                window_state=window_repo,
                alerts=_make_alerts(),
                clock=_make_clock(),
            ),
            trade_repo,
            window_repo,
        )

    def _paper_trade(self, direction="UP", stake=10.0, entry=0.65, window_ts="1776109200"):
        return {
            "id": "t001", "order_id": "5min-1234",
            "direction": direction, "stake_usd": stake,
            "entry_price": entry, "execution_mode": "paper",
            "metadata": f'{{"window_ts": "{window_ts}"}}',
            "asset": "BTC", "window_ts": window_ts, "created_at": None,
        }

    def test_win_when_direction_matches(self):
        trade = self._paper_trade(direction="UP")
        uc, trade_repo, _ = self._make_uc(
            paper_trades=[trade], actual_direction="UP"
        )
        resolved, skipped, _ = asyncio.run(uc._resolve_paper_batch())
        assert resolved == 1
        assert skipped == 0
        trade_repo.resolve_trade.assert_awaited_once()
        call_kwargs = trade_repo.resolve_trade.call_args.kwargs
        assert call_kwargs["outcome"] == "WIN"
        assert call_kwargs["status"] == "RESOLVED_WIN"

    def test_loss_when_direction_mismatches(self):
        trade = self._paper_trade(direction="UP")
        uc, trade_repo, _ = self._make_uc(
            paper_trades=[trade], actual_direction="DOWN"
        )
        resolved, skipped, _ = asyncio.run(uc._resolve_paper_batch())
        assert resolved == 1
        call_kwargs = trade_repo.resolve_trade.call_args.kwargs
        assert call_kwargs["outcome"] == "LOSS"
        assert call_kwargs["pnl_usd"] == round(-10.0, 4)

    def test_skips_when_oracle_not_resolved(self):
        trade = self._paper_trade()
        uc, trade_repo, _ = self._make_uc(
            paper_trades=[trade], actual_direction=None
        )
        resolved, skipped, _ = asyncio.run(uc._resolve_paper_batch())
        assert resolved == 0
        assert skipped == 1
        trade_repo.resolve_trade.assert_not_awaited()

    def test_skips_when_window_ts_missing(self):
        trade = self._paper_trade()
        trade["window_ts"] = None  # no window_ts
        uc, trade_repo, _ = self._make_uc(paper_trades=[trade])
        resolved, skipped, _ = asyncio.run(uc._resolve_paper_batch())
        assert resolved == 0
        assert skipped == 1

    def test_continues_after_individual_error(self):
        trade1 = self._paper_trade()
        trade2 = {**self._paper_trade(), "id": "t002"}
        uc, trade_repo, window_repo = self._make_uc(
            paper_trades=[trade1, trade2], actual_direction="UP"
        )
        # Make first resolve fail
        trade_repo.resolve_trade.side_effect = [Exception("DB down"), None]
        resolved, skipped, errors = asyncio.run(uc._resolve_paper_batch())
        assert resolved == 1  # second trade resolved
        assert skipped == 0
        assert errors == 1   # first trade errored

    def test_pnl_calculation_win(self):
        # stake=10, entry=0.5 → shares=20, pnl=20-10=10
        trade = self._paper_trade(direction="UP", stake=10.0, entry=0.5)
        uc, trade_repo, _ = self._make_uc(
            paper_trades=[trade], actual_direction="UP"
        )
        asyncio.run(uc._resolve_paper_batch())
        call_kwargs = trade_repo.resolve_trade.call_args.kwargs
        assert call_kwargs["pnl_usd"] == round(10.0, 4)

    def test_pnl_calculation_loss(self):
        # stake=10, loss → pnl=-10
        trade = self._paper_trade(direction="UP", stake=10.0, entry=0.5)
        uc, trade_repo, _ = self._make_uc(
            paper_trades=[trade], actual_direction="DOWN"
        )
        asyncio.run(uc._resolve_paper_batch())
        call_kwargs = trade_repo.resolve_trade.call_args.kwargs
        assert call_kwargs["pnl_usd"] == round(-10.0, 4)


# ---------------------------------------------------------------------------
# ReconcilePositionsUseCase.execute()
# ---------------------------------------------------------------------------

class TestExecute:

    def _make_uc(self, paper_trades=None, actual_direction="UP"):
        from use_cases.reconcile_positions import ReconcilePositionsUseCase
        trade_repo = _make_trade_repo(paper_trades=paper_trades or [])
        window_repo = _make_window_repo(actual_direction=actual_direction)
        uc = ReconcilePositionsUseCase(
            trade_repo=trade_repo,
            window_state=window_repo,
            alerts=_make_alerts(),
            clock=_make_clock(),
        )
        return uc, trade_repo

    def test_empty_positions_still_runs_paper_batch(self):
        from domain.value_objects import ReconcileResult
        paper_trade = {
            "id": "t001", "order_id": "5min-x", "direction": "DOWN",
            "stake_usd": 5.0, "entry_price": 0.6, "execution_mode": "paper",
            "metadata": '{"window_ts": "1776109200"}',
            "asset": "BTC", "window_ts": "1776109200", "created_at": None,
        }
        uc, trade_repo = self._make_uc(paper_trades=[paper_trade], actual_direction="DOWN")
        result = asyncio.run(uc.execute([]))
        assert isinstance(result, ReconcileResult)
        assert result.live_resolved == 0
        assert result.paper_resolved == 1

    def test_returns_reconcile_result_type(self):
        from domain.value_objects import ReconcileResult
        uc, _ = self._make_uc()
        result = asyncio.run(uc.execute([]))
        assert isinstance(result, ReconcileResult)

    def test_live_position_reaches_trade_repo_resolve(self):
        """Port contract: live positions must reach trade_repo.resolve_trade.
        Guards against #207-class stale-import silent-drop of TRADE path."""
        trade = _match()
        uc, trade_repo = self._make_uc()
        trade_repo.find_by_token_id.return_value = trade
        pos = _pos(outcome="WIN")

        from domain.value_objects import ReconcileResult
        result = asyncio.run(uc.execute([pos]))
        assert isinstance(result, ReconcileResult)
        assert result.live_resolved == 1
        trade_repo.resolve_trade.assert_awaited_once()

    def test_zero_positions_resolve_trade_not_called(self):
        """Port contract: no positions -> trade_repo.resolve_trade never called."""
        uc, trade_repo = self._make_uc()
        asyncio.run(uc.execute([]))
        trade_repo.resolve_trade.assert_not_awaited()

    def test_live_position_error_increments_errors(self):
        """Exception during live resolution is counted in ReconcileResult.errors."""
        uc, trade_repo = self._make_uc()
        # Make resolve_one raise by having find_by_token_id raise
        trade_repo.find_by_token_id.side_effect = RuntimeError("DB down")

        from domain.value_objects import ReconcileResult
        pos = _pos(outcome="WIN")
        result = asyncio.run(uc.execute([pos]))
        assert isinstance(result, ReconcileResult)
        assert result.errors >= 1

    def test_flush_alerts_sends_batched_message(self):
        """_flush_resolution_alerts sends batched message via send_raw_message.

        AsyncMock auto-creates any attribute access, so hasattr(mock, "send_raw_message")
        is always True — _flush_resolution_alerts always takes the send_raw_message branch
        in tests. Assert the exact path rather than a disjunction that never fails.
        """
        uc, trade_repo = self._make_uc()
        trade_repo.find_by_token_id.return_value = _match()
        alerts = uc._alerts

        # Execute with a live position to populate _pending_live_alerts
        pos = _pos(outcome="WIN")
        asyncio.run(uc.execute([pos]))

        # send_raw_message is the branch taken: hasattr(AsyncMock, anything) is True
        alerts.send_raw_message.assert_called_once()
        msg = alerts.send_raw_message.call_args.args[0]
        assert "Reconcile" in msg
        alerts.send_system_alert.assert_not_called()

    def test_flush_alerts_silent_when_no_alerts(self):
        """_flush_resolution_alerts is silent when no resolutions occurred."""
        uc, _ = self._make_uc()
        alerts = uc._alerts
        asyncio.run(uc.execute([]))  # No positions, no paper trades
        alerts.send_system_alert.assert_not_called()
        alerts.send_raw_message.assert_not_called()
