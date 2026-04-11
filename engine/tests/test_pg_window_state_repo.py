"""Tests for PgWindowRepository WindowStateRepository (CA-04)."""
from __future__ import annotations
import asyncio
from datetime import datetime
from typing import Any
import pytest

class MockConnection:
    def __init__(self):
        self.execute_calls = []
        self.fetchval_result = None
        self.fetch_result = []
    async def execute(self, q, *a): self.execute_calls.append((q, a))
    async def fetchval(self, q, *a): self.execute_calls.append((q, a)); return self.fetchval_result
    async def fetch(self, q, *a): self.execute_calls.append((q, a)); return self.fetch_result

class MockPool:
    def __init__(self, conn): self._conn = conn
    def acquire(self): return _Ctx(self._conn)

class _Ctx:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *a): pass

@pytest.fixture
def conn(): return MockConnection()
@pytest.fixture
def pool(conn): return MockPool(conn)
@pytest.fixture
def repo(pool):
    from engine.adapters.persistence.pg_window_repo import PgWindowRepository
    return PgWindowRepository(pool)

class TestWasTraded:
    def test_true(self, repo, conn):
        conn.fetchval_result = True
        assert asyncio.get_event_loop().run_until_complete(repo.was_traded("BTC-1234")) is True
    def test_false(self, repo, conn):
        conn.fetchval_result = False
        assert asyncio.get_event_loop().run_until_complete(repo.was_traded("BTC-9")) is False
    def test_no_pool(self):
        from engine.adapters.persistence.pg_window_repo import PgWindowRepository
        assert asyncio.get_event_loop().run_until_complete(PgWindowRepository(None).was_traded("X")) is False

class TestMarkTraded:
    def test_insert(self, repo, conn):
        asyncio.get_event_loop().run_until_complete(repo.mark_traded("BTC-1775683200", "0xabc"))
        q, a = conn.execute_calls[0]
        assert "INSERT INTO window_states" in q and a[0] == "BTC-1775683200" and a[1] == "BTC" and a[2] == 1775683200 and isinstance(a[3], datetime) and a[4] == "0xabc"
    def test_multi_dash(self, repo, conn):
        asyncio.get_event_loop().run_until_complete(repo.mark_traded("ETH-USD-12345", "o1"))
        assert conn.execute_calls[0][1][1] == "ETH-USD" and conn.execute_calls[0][1][2] == 12345

class TestWasResolved:
    def test_true(self, repo, conn):
        conn.fetchval_result = True
        assert asyncio.get_event_loop().run_until_complete(repo.was_resolved("BTC-1")) is True
    def test_false(self, repo, conn):
        conn.fetchval_result = False
        assert asyncio.get_event_loop().run_until_complete(repo.was_resolved("BTC-1")) is False

class TestMarkResolved:
    def test_update(self, repo, conn):
        asyncio.get_event_loop().run_until_complete(repo.mark_resolved("BTC-1234", "WIN"))
        q, a = conn.execute_calls[0]
        assert "UPDATE window_states" in q and a[2] == "WIN"

class TestLoadRecentTraded:
    def test_keys(self, repo, conn):
        conn.fetch_result = [{"window_key": "BTC-100"}, {"window_key": "BTC-200"}]
        assert asyncio.get_event_loop().run_until_complete(repo.load_recent_traded(2)) == {"BTC-100", "BTC-200"}
    def test_empty(self, repo, conn):
        conn.fetch_result = []
        assert asyncio.get_event_loop().run_until_complete(repo.load_recent_traded(4)) == set()

class TestEnsureTable:
    def test_creates(self, repo, conn):
        asyncio.get_event_loop().run_until_complete(repo.ensure_window_states_table())
        assert len(conn.execute_calls) == 4
        qs = [q for q, _ in conn.execute_calls]
        assert any("CREATE TABLE" in q for q in qs)
