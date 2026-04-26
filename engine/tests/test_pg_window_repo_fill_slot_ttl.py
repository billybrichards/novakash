"""Tests for the stale-placeholder TTL takeover (audit #398).

The PgWindowRepository.try_claim_fill_slot adapter writes a placeholder
row to ``strategy_window_fills`` BEFORE the order fires (audit #322).
On normal release paths execute_trade.execute deletes that placeholder;
audit #398 hardens the release path with a try/finally backstop.

Defence-in-depth at the SQL layer: the INSERT … ON CONFLICT DO UPDATE
clause STEALS the row when the existing one is still a placeholder
AND older than ``STALE_PLACEHOLDER_TTL_SECONDS``. Real fills (order_id
!= 'pending') are NEVER overwritten regardless of age.

These tests use a MockConnection (no real DB) and verify:
  1. Fresh INSERT path: fetchval returns 1 → caller wins.
  2. Loser path (active placeholder OR real fill): fetchval returns
     None → caller loses, returns False.
  3. SQL contains the TTL takeover clause so a real DB upgrade can't
     silently regress to a non-stealing placeholder.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


class MockConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_result: Any = None

    async def execute(self, q: str, *a: Any) -> None:
        self.execute_calls.append((q, a))

    async def fetchval(self, q: str, *a: Any) -> Any:
        self.execute_calls.append((q, a))
        return self.fetchval_result

    async def fetch(self, q: str, *a: Any) -> list:
        self.execute_calls.append((q, a))
        return []


class MockPool:
    def __init__(self, conn: MockConnection) -> None:
        self._conn = conn

    def acquire(self) -> "_Ctx":
        return _Ctx(self._conn)


class _Ctx:
    def __init__(self, c: MockConnection) -> None:
        self._c = c

    async def __aenter__(self) -> MockConnection:
        return self._c

    async def __aexit__(self, *a: Any) -> None:
        pass


def _make_repo() -> tuple[Any, MockConnection]:
    from adapters.persistence.pg_window_repo import PgWindowRepository

    conn = MockConnection()
    pool = MockPool(conn)
    return PgWindowRepository(pool), conn


def _make_key(window_ts: int = 1_777_240_500):
    from domain.value_objects import WindowKey

    return WindowKey(asset="BTC", window_ts=window_ts, timeframe="5m")


def test_try_claim_fill_slot_won_when_fresh_insert():
    """RETURNING 1 indicates the INSERT path fired (or stale-takeover
    matched). Caller wins the slot.
    """
    repo, conn = _make_repo()
    conn.fetchval_result = 1  # asyncpg returns the literal 1
    key = _make_key()

    won = asyncio.run(repo.try_claim_fill_slot(key, "v9_lgb_only"))
    assert won is True

    # Verify the SQL emitted carries the TTL takeover clause. We don't
    # parse SQL — substring match is enough to catch a refactor that
    # accidentally drops the stale-placeholder steal.
    sql = conn.execute_calls[0][0]
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql
    # The WHERE predicate scopes the takeover: only PLACEHOLDER rows
    # that have aged past the TTL are eligible. A real fill (order_id
    # set to a hex hash) MUST NOT be overwritten.
    assert "strategy_window_fills.order_id =" in sql
    assert "filled_at" in sql
    assert "interval" in sql.lower()


def test_try_claim_fill_slot_lost_when_active_placeholder():
    """When the existing row is an ACTIVE placeholder (not yet stale),
    the WHERE predicate evaluates False, DO UPDATE no-ops, RETURNING
    returns no row → fetchval is None → caller loses.

    Same shape applies to a real fill marker (order_id != 'pending').
    """
    repo, conn = _make_repo()
    conn.fetchval_result = None  # no row returned
    key = _make_key()

    won = asyncio.run(repo.try_claim_fill_slot(key, "v10_lgb_only"))
    assert won is False


def test_try_claim_fill_slot_no_pool_returns_true_failopen():
    """No DB → no pessimistic guard possible. Repo falls back to True
    so the lease (15s TTL) is the only protection. This branch is
    dead in production but locked in for parity with the existing
    contract used by tests that pass repo=None.
    """
    from adapters.persistence.pg_window_repo import PgWindowRepository

    repo = PgWindowRepository(pool=None)
    key = _make_key()
    won = asyncio.run(repo.try_claim_fill_slot(key, "v9_lgb_only"))
    assert won is True


def test_try_claim_fill_slot_empty_strategy_id_returns_true():
    """Defensive: an empty strategy_id collapses all callers into the
    same row, recreating audit #320. Repo refuses to bind that key —
    contract is "treat as no-op, defer to lease".
    """
    repo, _conn = _make_repo()
    key = _make_key()
    won = asyncio.run(repo.try_claim_fill_slot(key, ""))
    assert won is True


def test_try_claim_fill_slot_db_error_fails_closed():
    """Any DB exception → return False (don't fire FAK without a
    pessimistic guard). Caller surfaces a benign skip and tries again
    next eval tick.
    """
    repo, conn = _make_repo()

    async def _raise(*_a, **_kw):
        raise RuntimeError("DB unreachable")

    conn.fetchval = _raise  # type: ignore[assignment]

    key = _make_key()
    won = asyncio.run(repo.try_claim_fill_slot(key, "v9_lgb_only"))
    assert won is False


def test_release_fill_slot_logs_rows_deleted():
    """release_fill_slot returns asyncpg DELETE tag like 'DELETE 1'.
    Verifies the parser extracts the row count without raising; INFO
    log promotion in audit #398 surfaces this for forensics.
    """
    from adapters.persistence.pg_window_repo import PgWindowRepository

    conn = MockConnection()
    pool = MockPool(conn)

    async def _execute_with_tag(_q, *_a):
        return "DELETE 1"

    conn.execute = _execute_with_tag  # type: ignore[assignment]

    repo = PgWindowRepository(pool)
    key = _make_key()
    # Smoke-test: must not raise. Detailed log assertions live in the
    # forensics dashboards; this only verifies the parser path.
    asyncio.run(repo.release_fill_slot(key, "v9_lgb_only"))
