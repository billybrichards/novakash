"""Unit tests for CLOBReconciler.resolve_from_redeem.

Regression coverage for the v8_champion bug where every post-redeem
write-back silently returned 0 rows because ``resolve_from_redeem`` only
matched on ``metadata->>'condition_id'`` — and v8 trade metadata only
ever carries ``token_id`` + ``market_slug``, never ``condition_id``.

Mocks the asyncpg pool/connection so we can assert the exact SQL params
and side-effects without touching a real DB.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from reconciliation.reconciler import CLOBReconciler


class _FakeRow(dict):
    """asyncpg Record shim — access by column name like a Record."""

    def __getitem__(self, key):  # type: ignore[override]
        return super().__getitem__(key)


class _FakeConn:
    """Mimics asyncpg.Connection for fetch/execute with a scripted sequence."""

    def __init__(self, fetch_side_effect, execute_side_effect="UPDATE 1"):
        self.fetch = AsyncMock(side_effect=fetch_side_effect)
        # execute may be called N times (once per row) — accept list too
        if isinstance(execute_side_effect, list):
            self.execute = AsyncMock(side_effect=execute_side_effect)
        else:
            self.execute = AsyncMock(return_value=execute_side_effect)


class _FakePoolCtx:
    """Async context manager for pool.acquire()."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakePoolCtx(self._conn)


def _build_reconciler(conn):
    """Instantiate CLOBReconciler with the bare minimum for resolve_from_redeem."""
    return CLOBReconciler(
        poly_client=MagicMock(),
        db_pool=_FakePool(conn),
        alerter=MagicMock(),
        shutdown_event=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_tier1_condition_id_match_wins():
    """Happy path: condition_id matches, resolver updates the row and
    returns 1. Tier 2 fallback is NOT exercised."""
    # Tier 1 fetch returns one unresolved row
    fetch_calls = [[_FakeRow(id=42, stake_usd=5.0, outcome=None)]]
    conn = _FakeConn(fetch_side_effect=fetch_calls, execute_side_effect="UPDATE 1")
    r = _build_reconciler(conn)

    updated = await r.resolve_from_redeem(
        condition_id="0xcondition_id_for_v4",
        tx_hash="0xabc123",
        usdc_redeemed=10.0,
        token_id="0xshouldnt_be_used",
        outcome="WIN",
    )

    assert updated == 1
    # Exactly one fetch call — Tier 2 skipped because Tier 1 returned rows
    assert conn.fetch.await_count == 1
    # execute called once with outcome='WIN', status='RESOLVED_WIN',
    # pnl=10.0-5.0=5.0
    assert conn.execute.await_count == 1
    call_args = conn.execute.await_args_list[0].args
    # args: (sql, outcome, pnl, status, tx_hash, trade_id)
    assert call_args[1] == "WIN"
    assert call_args[2] == pytest.approx(5.0)
    assert call_args[3] == "RESOLVED_WIN"
    assert call_args[4] == "0xabc123"
    assert call_args[5] == 42


@pytest.mark.asyncio
async def test_tier2_token_id_fallback_for_v8_champion():
    """Regression: v8_champion metadata has no condition_id — resolver
    MUST fall back to token_id prefix match and still update the row."""
    # Tier 1 fetch returns empty (no condition_id in metadata)
    # Tier 2 fetch returns the v8 trade by token_id prefix
    fetch_calls = [
        [],  # Tier 1 — condition_id miss
        [_FakeRow(id=123, stake_usd=7.0, outcome=None)],  # Tier 2 — token_id hit
    ]
    conn = _FakeConn(fetch_side_effect=fetch_calls, execute_side_effect="UPDATE 1")
    r = _build_reconciler(conn)

    updated = await r.resolve_from_redeem(
        condition_id="0xcondition_not_in_metadata",
        tx_hash="0xdef456",
        usdc_redeemed=14.0,
        token_id="0xtoken_id_v8_champion",
        outcome="WIN",
    )

    assert updated == 1
    # Two fetch calls: Tier 1 miss, Tier 2 hit
    assert conn.fetch.await_count == 2
    # Tier 2 fetch was called with the token_id as $1
    tier2_args = conn.fetch.await_args_list[1].args
    assert tier2_args[1] == "0xtoken_id_v8_champion"
    # UPDATE fired with the v8 trade_id and WIN PnL = 14-7 = 7
    assert conn.execute.await_count == 1
    call_args = conn.execute.await_args_list[0].args
    assert call_args[1] == "WIN"
    assert call_args[2] == pytest.approx(7.0)
    assert call_args[3] == "RESOLVED_WIN"
    assert call_args[5] == 123


@pytest.mark.asyncio
async def test_tier2_loss_outcome_stamps_resolved_loss():
    """LOSS path: PnL = -stake, status = RESOLVED_LOSS, regardless of any
    usdc_redeemed dust value."""
    fetch_calls = [
        [],  # Tier 1 miss
        [_FakeRow(id=999, stake_usd=3.5, outcome=None)],  # Tier 2 hit
    ]
    conn = _FakeConn(fetch_side_effect=fetch_calls, execute_side_effect="UPDATE 1")
    r = _build_reconciler(conn)

    updated = await r.resolve_from_redeem(
        condition_id="0xlost_market",
        tx_hash="0xloss_tx",
        usdc_redeemed=0.0,
        token_id="0xloss_token",
        outcome="LOSS",
    )

    assert updated == 1
    call_args = conn.execute.await_args_list[0].args
    assert call_args[1] == "LOSS"
    assert call_args[2] == pytest.approx(-3.5)
    assert call_args[3] == "RESOLVED_LOSS"


@pytest.mark.asyncio
async def test_no_match_returns_zero():
    """Both tiers miss → return 0, no UPDATE fired."""
    fetch_calls = [[], []]  # Tier 1 + Tier 2 both empty
    conn = _FakeConn(fetch_side_effect=fetch_calls)
    r = _build_reconciler(conn)

    updated = await r.resolve_from_redeem(
        condition_id="0xorphan",
        tx_hash="0x",
        usdc_redeemed=1.0,
        token_id="0xalso_orphan",
        outcome="WIN",
    )

    assert updated == 0
    assert conn.execute.await_count == 0


@pytest.mark.asyncio
async def test_already_resolved_race_skips_update():
    """If UC beat us to it (row has non-NULL outcome), we log and skip
    the UPDATE — idempotency guard in action."""
    fetch_calls = [
        [_FakeRow(id=55, stake_usd=5.0, outcome="WIN")],  # already resolved
    ]
    conn = _FakeConn(fetch_side_effect=fetch_calls)
    r = _build_reconciler(conn)

    updated = await r.resolve_from_redeem(
        condition_id="0xalready_done",
        tx_hash="0x",
        usdc_redeemed=10.0,
        token_id="",
        outcome="WIN",
    )

    # Already-resolved rows are counted as race hits, not updates
    assert updated == 0
    assert conn.execute.await_count == 0


@pytest.mark.asyncio
async def test_tier2_skipped_when_token_id_empty():
    """Tier 1 miss + empty token_id → no Tier 2 attempt → return 0."""
    fetch_calls = [[]]  # Only Tier 1 — Tier 2 must NOT be called
    conn = _FakeConn(fetch_side_effect=fetch_calls)
    r = _build_reconciler(conn)

    updated = await r.resolve_from_redeem(
        condition_id="0xnothing",
        tx_hash="0x",
        usdc_redeemed=0.0,
        token_id="",
        outcome="WIN",
    )

    assert updated == 0
    assert conn.fetch.await_count == 1  # Only Tier 1
