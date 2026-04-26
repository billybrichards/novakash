"""Tests for window_claims lease-based dedup (audit #316, #317).

Verifies the two-table dedup design:
  - window_claims (transient leases, TTL-bounded)
  - window_states (terminal fills, real order_id)

Plus the legacy shim that maps the old try_claim_trade/clear_trade_claim API
onto the new lease primitives.
"""
from __future__ import annotations

import asyncio
from typing import Any
import pytest


# ── Mock infra ──────────────────────────────────────────────────────────────


class MockConnection:
    """Records SQL calls + returns scripted results.

    fetchrow_result is a list — pop(0) per call so we can script multi-call
    sequences (e.g. for the lease takeover path).
    """

    def __init__(self):
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetchval_result = None
        self.fetchrow_result: list[Any] = []
        self.fetch_result: list[Any] = []

    async def execute(self, q, *a):
        self.execute_calls.append((q, a))

    async def fetchval(self, q, *a):
        self.execute_calls.append((q, a))
        return self.fetchval_result

    async def fetchrow(self, q, *a):
        self.execute_calls.append((q, a))
        if self.fetchrow_result:
            return self.fetchrow_result.pop(0)
        return None

    async def fetch(self, q, *a):
        self.execute_calls.append((q, a))
        return self.fetch_result


class _Ctx:
    def __init__(self, c):
        self._c = c

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a):
        pass


class MockPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Ctx(self._conn)


@pytest.fixture
def conn():
    return MockConnection()


@pytest.fixture
def pool(conn):
    return MockPool(conn)


@pytest.fixture
def repo(pool):
    from adapters.persistence.pg_window_repo import PgWindowRepository

    r = PgWindowRepository(pool)
    # Reset the legacy shim map between tests (instance-level dict on the
    # class — shared across tests would leak otherwise).
    r._legacy_claim_ids = {}
    return r


def make_key(asset="BTC", window_ts=1777200000, timeframe="5m"):
    from domain.value_objects import WindowKey

    return WindowKey(asset=asset, window_ts=window_ts, timeframe=timeframe)


# ── acquire_lease ───────────────────────────────────────────────────────────


class TestAcquireLease:
    def test_success_returns_claim_id(self, repo, conn):
        # First INSERT succeeds — RETURNING returns claim_id + attempt_n=1
        conn.fetchrow_result = [
            {"claim_id": "abcd1234-aaaa-bbbb-cccc-1234567890ab", "attempt_n": 1}
        ]
        key = make_key()
        claim_id = asyncio.run(repo.acquire_lease(key, claimed_by="v9_lgb_only"))
        assert claim_id == "abcd1234-aaaa-bbbb-cccc-1234567890ab"
        # SQL went to the right table with correct semantics
        q, a = conn.execute_calls[0]
        assert "INSERT INTO window_claims" in q
        assert "ON CONFLICT" in q
        assert "expires_at < $6" in q  # only steal if expired
        assert a[0] == "BTC"
        assert a[1] == 1777200000
        assert a[4] == "v9_lgb_only"  # claimed_by

    def test_busy_returns_none(self, repo, conn):
        # Active lease held by someone else: INSERT...ON CONFLICT WHERE
        # expires_at < now() is false → no UPDATE → RETURNING empty → None
        conn.fetchrow_result = [None]
        key = make_key()
        claim_id = asyncio.run(repo.acquire_lease(key))
        assert claim_id is None

    def test_steal_expired_lease(self, repo, conn):
        # Lease was held by dead process (expired). ON CONFLICT DO UPDATE
        # fires, attempt_n increments to 2 — we get a fresh claim_id.
        conn.fetchrow_result = [
            {"claim_id": "ffff0000-aaaa-bbbb-cccc-deadbeef0000", "attempt_n": 2}
        ]
        key = make_key()
        claim_id = asyncio.run(repo.acquire_lease(key))
        assert claim_id == "ffff0000-aaaa-bbbb-cccc-deadbeef0000"

    def test_pool_less_returns_synthetic_id(self, conn):
        from adapters.persistence.pg_window_repo import PgWindowRepository

        repo = PgWindowRepository(pool=None)
        result = asyncio.run(repo.acquire_lease(make_key()))
        assert isinstance(result, str)
        assert len(result) > 8  # uuid-like

    def test_db_failure_returns_none(self, repo, conn):
        # Simulate driver exception — should fail closed (return None)
        async def boom(*a, **kw):
            raise RuntimeError("connection lost")

        conn.fetchrow = boom
        result = asyncio.run(repo.acquire_lease(make_key()))
        assert result is None


# ── release_lease ───────────────────────────────────────────────────────────


class TestReleaseLease:
    def test_releases_with_matching_claim_id(self, repo, conn):
        key = make_key()
        asyncio.run(repo.release_lease(key, "abcd1234-aaaa-bbbb-cccc-1234567890ab"))
        q, a = conn.execute_calls[0]
        assert "DELETE FROM window_claims" in q
        # claim_id is included in WHERE clause — prevents clobbering
        # successor leases
        assert "claim_id = $4::uuid" in q
        assert a[3] == "abcd1234-aaaa-bbbb-cccc-1234567890ab"

    def test_pool_less_no_op(self):
        from adapters.persistence.pg_window_repo import PgWindowRepository

        repo = PgWindowRepository(pool=None)
        # Should not raise
        asyncio.run(repo.release_lease(make_key(), "any-claim-id"))


# ── Legacy shim: try_claim_trade / clear_trade_claim ────────────────────────


class TestLegacyShim:
    def test_try_claim_trade_returns_true_on_acquire(self, repo, conn):
        conn.fetchrow_result = [{"claim_id": "x" * 8 + "-aaaa-bbbb-cccc-" + "1" * 12, "attempt_n": 1}]
        key = make_key()
        assert asyncio.run(repo.try_claim_trade(key)) is True
        # claim_id remembered for clear_trade_claim
        slot = (key.asset, key.window_ts, key.timeframe)
        assert slot in repo._legacy_claim_ids

    def test_try_claim_trade_returns_false_on_busy(self, repo, conn):
        conn.fetchrow_result = [None]
        key = make_key()
        assert asyncio.run(repo.try_claim_trade(key)) is False
        # nothing remembered when claim wasn't acquired
        slot = (key.asset, key.window_ts, key.timeframe)
        assert slot not in repo._legacy_claim_ids

    def test_clear_trade_claim_releases(self, repo, conn):
        # Pre-populate the shim map (simulates a prior try_claim_trade)
        key = make_key()
        slot = (key.asset, key.window_ts, key.timeframe)
        claim_id = "deadbeef-aaaa-bbbb-cccc-deadbeef0000"
        repo._legacy_claim_ids[slot] = claim_id

        asyncio.run(repo.clear_trade_claim(key))
        # DELETE was called with the right claim_id
        q, a = conn.execute_calls[0]
        assert "DELETE FROM window_claims" in q
        assert a[3] == claim_id
        # Map cleaned up
        assert slot not in repo._legacy_claim_ids

    def test_clear_trade_claim_no_op_without_memory(self, repo, conn):
        # No prior try_claim_trade — clear should silently do nothing
        # (lease will expire naturally)
        key = make_key()
        asyncio.run(repo.clear_trade_claim(key))
        # No DB call made
        assert len(conn.execute_calls) == 0


# ── Schema migration ────────────────────────────────────────────────────────


class TestEnsureTable:
    def test_creates_both_tables(self, repo, conn):
        asyncio.run(repo.ensure_window_states_table())
        qs = [q for q, _ in conn.execute_calls]
        # Old table
        assert any("CREATE TABLE IF NOT EXISTS window_states" in q for q in qs)
        # New table for leases
        assert any("CREATE TABLE IF NOT EXISTS window_claims" in q for q in qs)
        # Index on lease expiry for janitor + diagnostic queries
        assert any("idx_window_claims_expires_at" in q for q in qs)

    def test_window_claims_pk_and_columns(self, repo, conn):
        asyncio.run(repo.ensure_window_states_table())
        qs = [q for q, _ in conn.execute_calls]
        claims_ddl = next(q for q in qs if "window_claims" in q and "CREATE" in q)
        assert "claim_id UUID NOT NULL" in claims_ddl
        assert "expires_at TIMESTAMPTZ NOT NULL" in claims_ddl
        assert "attempt_n INT NOT NULL DEFAULT 1" in claims_ddl
        assert "PRIMARY KEY (asset, window_ts, timeframe)" in claims_ddl


# ── End-to-end dedup invariants (the real protection target) ────────────────


class TestDedupInvariants:
    """The whole point of this design — verify no double-fill, no lock-out."""

    def test_two_concurrent_claims_one_wins(self, repo, conn):
        # Two acquire_lease calls back-to-back. First gets the lease; second
        # should see the active row and return None.
        conn.fetchrow_result = [
            {"claim_id": "11111111-aaaa-bbbb-cccc-111111111111", "attempt_n": 1},
            None,  # second call: ON CONFLICT WHERE expires_at < now() is false
        ]
        key = make_key()
        first = asyncio.run(repo.acquire_lease(key, claimed_by="A"))
        second = asyncio.run(repo.acquire_lease(key, claimed_by="B"))
        assert first is not None
        assert second is None

    def test_failed_attempt_releases_for_retry(self, repo, conn):
        # Attempt 1: claim acquired, FAK fails, release lease
        # Attempt 2: claim acquired again (window now free)
        conn.fetchrow_result = [
            {"claim_id": "11111111-aaaa-bbbb-cccc-111111111111", "attempt_n": 1},
            {"claim_id": "22222222-aaaa-bbbb-cccc-222222222222", "attempt_n": 1},
        ]
        key = make_key()
        first_claim = asyncio.run(repo.acquire_lease(key))
        asyncio.run(repo.release_lease(key, first_claim))
        second_claim = asyncio.run(repo.acquire_lease(key))
        assert first_claim and second_claim
        assert first_claim != second_claim

    def test_expired_lease_stolen_with_new_attempt_n(self, repo, conn):
        # Initial claim succeeds, then "process dies" (we don't release).
        # Next acquire should steal — attempt_n=2 indicates takeover.
        conn.fetchrow_result = [
            {"claim_id": "33333333-aaaa-bbbb-cccc-333333333333", "attempt_n": 2}
        ]
        key = make_key()
        claim_id = asyncio.run(repo.acquire_lease(key, claimed_by="successor"))
        assert claim_id == "33333333-aaaa-bbbb-cccc-333333333333"
        # The presence of attempt_n>1 in the result is what triggers the
        # "steal" log line in the code — verifies lease takeover semantics.
