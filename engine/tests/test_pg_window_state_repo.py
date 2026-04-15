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
        assert asyncio.get_event_loop().run_until_complete(repo.was_traded(key)) is True

    def test_false(self, repo, conn):
        conn.fetchval_result = False
        key = make_window_key("BTC", 9, "5m")
        assert (
            asyncio.get_event_loop().run_until_complete(repo.was_traded(key)) is False
        )

    def test_no_pool(self):
        from adapters.persistence.pg_window_repo import PgWindowRepository

        key = make_window_key("X", 1, "5m")
        assert (
            asyncio.get_event_loop().run_until_complete(
                PgWindowRepository(None).was_traded(key)
            )
            is False
        )


class TestMarkTraded:
    def test_insert(self, repo, conn):
        key = make_window_key("BTC", 1775683200, "5m")
        asyncio.get_event_loop().run_until_complete(repo.mark_traded(key, "0xabc"))
        q, a = conn.execute_calls[0]
        assert "INSERT INTO window_states" in q
        assert a[0] == "BTC"
        assert a[1] == 1775683200
        assert a[2] == "5m"
        assert isinstance(a[3], datetime)
        assert a[4] == "0xabc"

    def test_multi_asset(self, repo, conn):
        key = make_window_key("ETH", 12345, "5m")
        asyncio.get_event_loop().run_until_complete(repo.mark_traded(key, "o1"))
        q, a = conn.execute_calls[0]
        assert a[0] == "ETH"
        assert a[1] == 12345
        assert a[2] == "5m"


class TestClaims:
    def test_try_claim_true(self, repo, conn):
        conn.fetchval_result = 1
        key = make_window_key("BTC", 1234, "5m")
        assert (
            asyncio.get_event_loop().run_until_complete(repo.try_claim_trade(key))
            is True
        )

    def test_try_claim_false(self, repo, conn):
        conn.fetchval_result = None
        key = make_window_key("BTC", 1234, "5m")
        assert (
            asyncio.get_event_loop().run_until_complete(repo.try_claim_trade(key))
            is False
        )

    def test_clear_claim(self, repo, conn):
        key = make_window_key("BTC", 1234, "5m")
        asyncio.get_event_loop().run_until_complete(repo.clear_trade_claim(key))
        q, a = conn.execute_calls[0]
        assert "DELETE FROM window_states" in q
        assert a[0] == "BTC"
        assert a[1] == 1234
        assert a[2] == "5m"


class TestWasResolved:
    def test_true(self, repo, conn):
        conn.fetchval_result = True
        key = make_window_key("BTC", 1, "5m")
        assert (
            asyncio.get_event_loop().run_until_complete(repo.was_resolved(key)) is True
        )

    def test_false(self, repo, conn):
        conn.fetchval_result = False
        key = make_window_key("BTC", 1, "5m")
        assert (
            asyncio.get_event_loop().run_until_complete(repo.was_resolved(key)) is False
        )


class TestMarkResolved:
    def test_update(self, repo, conn):
        from domain.value_objects import WindowOutcome

        key = make_window_key("BTC", 1234, "5m")
        asyncio.get_event_loop().run_until_complete(
            repo.mark_resolved(key, WindowOutcome.UP)
        )
        q, a = conn.execute_calls[0]
        assert "UPDATE window_states" in q
        assert a[0] == "BTC"
        assert a[1] == 1234
        assert a[2] == "5m"
        assert a[3] == "UP"


class TestLoadRecentTraded:
    def test_keys(self, repo, conn):
        from domain.value_objects import WindowKey

        conn.fetch_result = [
            {"asset": "BTC", "window_ts": 100, "timeframe": "5m"},
            {"asset": "BTC", "window_ts": 200, "timeframe": "5m"},
        ]
        result = asyncio.get_event_loop().run_until_complete(repo.load_recent_traded(2))
        assert result == {WindowKey("BTC", 100, "5m"), WindowKey("BTC", 200, "5m")}

    def test_empty(self, repo, conn):
        conn.fetch_result = []
        assert (
            asyncio.get_event_loop().run_until_complete(repo.load_recent_traded(4))
            == set()
        )


class TestEnsureTable:
    def test_creates(self, repo, conn):
        asyncio.get_event_loop().run_until_complete(repo.ensure_window_states_table())
        assert len(conn.execute_calls) == 3
        qs = [q for q, _ in conn.execute_calls]
        assert any("CREATE TABLE" in q for q in qs)
