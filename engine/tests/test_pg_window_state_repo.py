"""Tests for PgWindowRepository WindowStateRepository (CA-04)."""

from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Any
import pytest


class MockConnection:
    def __init__(self):
        self.execute_calls = []
        self.fetchval_result = None
        self.fetch_result = []

    async def execute(self, q, *a):
        self.execute_calls.append((q, a))

    async def fetchval(self, q, *a):
        self.execute_calls.append((q, a))
        return self.fetchval_result

    async def fetch(self, q, *a):
        self.execute_calls.append((q, a))
        return self.fetch_result


class MockPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Ctx(self._conn)


class _Ctx:
    def __init__(self, c):
        self._c = c

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a):
        pass


@pytest.fixture
def conn():
    return MockConnection()


@pytest.fixture
def pool(conn):
    return MockPool(conn)


@pytest.fixture
def repo(pool):
    from adapters.persistence.pg_window_repo import PgWindowRepository

    return PgWindowRepository(pool)


def make_window_key(asset="BTC", window_ts=1234, timeframe="5m"):
    from domain.value_objects import WindowKey

    return WindowKey(asset=asset, window_ts=window_ts, timeframe=timeframe)


class TestWasTraded:
    def test_true(self, repo, conn):
        conn.fetchval_result = True
        key = make_window_key("BTC", 1234, "5m")
        assert asyncio.run(repo.was_traded(key)) is True

    def test_false(self, repo, conn):
        conn.fetchval_result = False
        key = make_window_key("BTC", 9, "5m")
        assert asyncio.run(repo.was_traded(key)) is False

    def test_no_pool(self):
        from adapters.persistence.pg_window_repo import PgWindowRepository

        key = make_window_key("X", 1, "5m")
        assert asyncio.run(PgWindowRepository(None).was_traded(key)) is False


class TestMarkTraded:
    def test_insert(self, repo, conn):
        key = make_window_key("BTC", 1775683200, "5m")
        asyncio.run(repo.mark_traded(key, "0xabc"))
        q, a = conn.execute_calls[0]
        assert "INSERT INTO window_states" in q
        assert a[0] == "BTC"
        assert a[1] == 1775683200
        assert a[2] == "5m"
        assert isinstance(a[3], datetime)
        assert a[4] == "0xabc"

    def test_multi_asset(self, repo, conn):
        key = make_window_key("ETH", 12345, "5m")
        asyncio.run(repo.mark_traded(key, "o1"))
        q, a = conn.execute_calls[0]
        assert a[0] == "ETH"
        assert a[1] == 12345
        assert a[2] == "5m"


class TestClaims:
    """try_claim_trade / clear_trade_claim tests.

    Audit #320 (2026-04-26): per-strategy lease keys, claim_id explicit.
    See test_window_claims_lease.py for the full new lease-based test
    suite including the 3-strategy race regression.
    """

    def test_try_claim_returns_tuple_on_acquire(self, repo, conn):
        async def fetchrow(q, *a):
            conn.execute_calls.append((q, a))
            return {"claim_id": "11111111-aaaa-bbbb-cccc-111111111111", "attempt_n": 1}

        conn.fetchrow = fetchrow
        key = make_window_key("BTC", 1234, "5m")
        ok, claim_id = asyncio.run(
            repo.try_claim_trade(key, strategy_id="v9_lgb_only")
        )
        assert ok is True
        assert claim_id == "11111111-aaaa-bbbb-cccc-111111111111"

    def test_try_claim_returns_false_none_on_busy(self, repo, conn):
        async def fetchrow(q, *a):
            conn.execute_calls.append((q, a))
            return None  # active lease for the same strategy → no row returned

        conn.fetchrow = fetchrow
        key = make_window_key("BTC", 1234, "5m")
        ok, claim_id = asyncio.run(
            repo.try_claim_trade(key, strategy_id="v9_lgb_only")
        )
        assert ok is False
        assert claim_id is None

    def test_clear_claim_uses_explicit_claim_id(self, repo, conn):
        # Audit #320: claim_id is now plumbed explicitly. No more shim
        # side-state to clobber.
        key = make_window_key("BTC", 1234, "5m")
        claim_id = "deadbeef-aaaa-bbbb-cccc-deadbeef0000"
        asyncio.run(repo.clear_trade_claim(key, claim_id))
        q, a = conn.execute_calls[0]
        assert "DELETE FROM window_claims" in q
        assert a[0] == "BTC"
        assert a[1] == 1234
        assert a[2] == "5m"
        assert a[3] == claim_id


class TestWasResolved:
    def test_true(self, repo, conn):
        conn.fetchval_result = True
        key = make_window_key("BTC", 1, "5m")
        assert asyncio.run(repo.was_resolved(key)) is True

    def test_false(self, repo, conn):
        conn.fetchval_result = False
        key = make_window_key("BTC", 1, "5m")
        assert asyncio.run(repo.was_resolved(key)) is False


class TestMarkResolved:
    def test_update(self, repo, conn):
        from domain.value_objects import WindowOutcome
        from datetime import datetime, timezone

        key = make_window_key("BTC", 1234, "5m")
        asyncio.run(repo.mark_resolved(key, WindowOutcome.UP))
        q, a = conn.execute_calls[0]
        assert "UPDATE window_states" in q
        # SQL: asset=$1, resolved_at=$2, outcome=$3, window_ts=$4, timeframe=$5
        assert a[0] == "BTC"
        assert isinstance(a[1], datetime)  # resolved_at timestamp
        assert a[2] == "UP"               # outcome string
        assert a[3] == 1234               # window_ts
        assert a[4] == "5m"               # timeframe


class TestLoadRecentTraded:
    def test_keys(self, repo, conn):
        from domain.value_objects import WindowKey

        conn.fetch_result = [
            {"asset": "BTC", "window_ts": 100, "timeframe": "5m"},
            {"asset": "BTC", "window_ts": 200, "timeframe": "5m"},
        ]
        result = asyncio.run(repo.load_recent_traded(2))
        assert result == {WindowKey("BTC", 100, "5m"), WindowKey("BTC", 200, "5m")}

    def test_empty(self, repo, conn):
        conn.fetch_result = []
        assert asyncio.run(repo.load_recent_traded(4)) == set()


class TestEnsureTable:
    def test_creates(self, repo, conn):
        asyncio.run(repo.ensure_window_states_table())
        # 3 calls for window_states + 4 for window_claims:
        #   - CREATE TABLE
        #   - ALTER ADD COLUMN strategy_id (idempotent migration)
        #   - DO $$ migration block (idempotent PK swap)
        #   - CREATE INDEX
        # Total: 7
        assert len(conn.execute_calls) == 7
        qs = [q for q, _ in conn.execute_calls]
        assert any("CREATE TABLE IF NOT EXISTS window_states" in q for q in qs)
        assert any("CREATE TABLE IF NOT EXISTS window_claims" in q for q in qs)
        # Audit #320 migration: must add strategy_id col + swap PK
        assert any("ADD COLUMN IF NOT EXISTS strategy_id" in q for q in qs)
        assert any("DROP CONSTRAINT" in q and "ADD PRIMARY KEY" in q for q in qs)
