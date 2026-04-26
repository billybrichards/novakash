"""Tests for window_claims lease-based dedup (audit #316, #317, #320).

Verifies the two-table dedup design with PER-STRATEGY lease keys:
  - window_claims (transient leases, TTL-bounded, keyed per strategy)
  - window_states (terminal fills, real order_id)

Audit #320 (2026-04-26) — root-cause fix:
  Pre-#320 the lease key was (asset, window_ts, timeframe) and a class-
  level dict ``_legacy_claim_ids`` memoised claim_ids per call. Sibling
  strategies (v9_lgb_only, v9_ensemble, v10_lgb_only, …) running on the
  same window all fought for the same row + clobbered each other's
  claim_ids in the dict. clear_trade_claim then released the WRONG
  lease (or no-op'd), causing 11+ consecutive ``rows_deleted=0`` logs
  and 0 trades for 6 hours straight in production.

Post-#320:
  - PK is (asset, window_ts, timeframe, strategy_id) — siblings get
    independent rows, no contention.
  - try_claim_trade returns (bool, claim_id) explicitly.
  - clear_trade_claim takes claim_id explicitly. No more side-table.
  - The legacy ``_legacy_claim_ids`` shim is gone.
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

    return PgWindowRepository(pool)


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
        claim_id = asyncio.run(
            repo.acquire_lease(key, strategy_id="v9_lgb_only")
        )
        assert claim_id == "abcd1234-aaaa-bbbb-cccc-1234567890ab"
        # SQL went to the right table with correct semantics
        q, a = conn.execute_calls[0]
        assert "INSERT INTO window_claims" in q
        assert "ON CONFLICT" in q
        assert "(asset, window_ts, timeframe, strategy_id)" in q
        assert "expires_at < $7" in q  # only steal if expired (was $6 pre-#320)
        assert a[0] == "BTC"
        assert a[1] == 1777200000
        assert a[3] == "v9_lgb_only"  # strategy_id is in the PK

    def test_busy_returns_none(self, repo, conn):
        # Active lease held by same strategy: INSERT...ON CONFLICT WHERE
        # expires_at < now() is false → no UPDATE → RETURNING empty → None
        conn.fetchrow_result = [None]
        key = make_key()
        claim_id = asyncio.run(
            repo.acquire_lease(key, strategy_id="v9_lgb_only")
        )
        assert claim_id is None

    def test_steal_expired_lease(self, repo, conn):
        # Lease was held by dead process (expired). ON CONFLICT DO UPDATE
        # fires, attempt_n increments to 2 — we get a fresh claim_id.
        conn.fetchrow_result = [
            {"claim_id": "ffff0000-aaaa-bbbb-cccc-deadbeef0000", "attempt_n": 2}
        ]
        key = make_key()
        claim_id = asyncio.run(
            repo.acquire_lease(key, strategy_id="v9_lgb_only")
        )
        assert claim_id == "ffff0000-aaaa-bbbb-cccc-deadbeef0000"

    def test_pool_less_returns_synthetic_id(self, conn):
        from adapters.persistence.pg_window_repo import PgWindowRepository

        repo = PgWindowRepository(pool=None)
        result = asyncio.run(
            repo.acquire_lease(make_key(), strategy_id="v9_lgb_only")
        )
        assert isinstance(result, str)
        assert len(result) > 8  # uuid-like

    def test_db_failure_returns_none(self, repo, conn):
        # Simulate driver exception — should fail closed (return None)
        async def boom(*a, **kw):
            raise RuntimeError("connection lost")

        conn.fetchrow = boom
        result = asyncio.run(
            repo.acquire_lease(make_key(), strategy_id="v9_lgb_only")
        )
        assert result is None

    def test_missing_strategy_id_returns_none(self, repo, conn):
        """Audit #320 safety: an empty strategy_id would collapse all
        callers into the same row again, recreating the original bug.
        Surface immediately rather than silently corrupt the lease table.
        """
        result = asyncio.run(repo.acquire_lease(make_key(), strategy_id=""))
        assert result is None
        # No SQL should have been issued
        assert len(conn.execute_calls) == 0


# ── release_lease ───────────────────────────────────────────────────────────


class TestReleaseLease:
    def test_releases_with_matching_claim_id(self, repo, conn):
        key = make_key()
        asyncio.run(repo.release_lease(key, "abcd1234-aaaa-bbbb-cccc-1234567890ab"))
        q, a = conn.execute_calls[0]
        assert "DELETE FROM window_claims" in q
        # claim_id is included in WHERE clause — prevents clobbering
        # successor leases. claim_id alone is globally unique (UUID),
        # so we don't need strategy_id here.
        assert "claim_id = $4::uuid" in q
        assert a[3] == "abcd1234-aaaa-bbbb-cccc-1234567890ab"

    def test_pool_less_no_op(self):
        from adapters.persistence.pg_window_repo import PgWindowRepository

        repo = PgWindowRepository(pool=None)
        # Should not raise
        asyncio.run(repo.release_lease(make_key(), "any-claim-id"))

    def test_no_claim_id_logs_warning_and_returns(self, repo, conn):
        """release_lease called with empty claim_id should warn and no-op
        rather than emit a malformed DELETE."""
        asyncio.run(repo.release_lease(make_key(), ""))
        # No DELETE should have been issued
        delete_calls = [
            c for c in conn.execute_calls if "DELETE FROM window_claims" in c[0]
        ]
        assert len(delete_calls) == 0


# ── try_claim_trade / clear_trade_claim (per-strategy, claim_id explicit) ───


class TestTryClaimTrade:
    """Audit #320 (2026-04-26): try_claim_trade now returns (bool, claim_id)
    and is keyed per-strategy. The legacy in-memory shim is gone.
    """

    def test_returns_true_and_claim_id_on_acquire(self, repo, conn):
        conn.fetchrow_result = [
            {"claim_id": "abcd1234-aaaa-bbbb-cccc-deadbeef0000", "attempt_n": 1}
        ]
        key = make_key()
        ok, claim_id = asyncio.run(
            repo.try_claim_trade(key, strategy_id="v9_lgb_only")
        )
        assert ok is True
        assert claim_id == "abcd1234-aaaa-bbbb-cccc-deadbeef0000"

    def test_returns_false_none_on_busy(self, repo, conn):
        conn.fetchrow_result = [None]
        key = make_key()
        ok, claim_id = asyncio.run(
            repo.try_claim_trade(key, strategy_id="v9_lgb_only")
        )
        assert ok is False
        assert claim_id is None

    def test_passes_strategy_id_to_acquire(self, repo, conn):
        conn.fetchrow_result = [
            {"claim_id": "11111111-aaaa-bbbb-cccc-111111111111", "attempt_n": 1}
        ]
        key = make_key()
        asyncio.run(repo.try_claim_trade(key, strategy_id="v10_lgb_only"))
        # The INSERT positional arg #4 (1-indexed) is strategy_id
        q, a = conn.execute_calls[0]
        assert "INSERT INTO window_claims" in q
        assert a[3] == "v10_lgb_only"


class TestClearTradeClaim:
    def test_releases_with_explicit_claim_id(self, repo, conn):
        key = make_key()
        claim_id = "deadbeef-aaaa-bbbb-cccc-deadbeef0000"
        asyncio.run(repo.clear_trade_claim(key, claim_id))
        # DELETE was called with the right claim_id
        delete_calls = [
            c for c in conn.execute_calls if "DELETE FROM window_claims" in c[0]
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0][1][3] == claim_id

    def test_no_claim_id_logs_warning_and_returns(self, repo, conn):
        # Audit #320: pre-fix this was a silent no-op via the in-memory
        # shim. Now: WARNING-level log so state-machine bugs surface.
        key = make_key()
        asyncio.run(repo.clear_trade_claim(key, None))
        # No DB call made — lease will TTL out naturally
        assert len(conn.execute_calls) == 0

    def test_default_claim_id_argument_is_none(self, repo, conn):
        """clear_trade_claim must accept claim_id=None as default for
        accidental callers that haven't been updated. Pre-fix would
        silently no-op via the shim; post-fix logs a warning."""
        key = make_key()
        # Calling without claim_id at all should not raise
        asyncio.run(repo.clear_trade_claim(key))
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

    def test_window_claims_pk_includes_strategy_id(self, repo, conn):
        """Audit #320 invariant: PK MUST include strategy_id, otherwise
        sibling strategies fight for the same row."""
        asyncio.run(repo.ensure_window_states_table())
        qs = [q for q, _ in conn.execute_calls]
        claims_ddl = next(q for q in qs if "window_claims" in q and "CREATE" in q)
        assert "claim_id UUID NOT NULL" in claims_ddl
        assert "expires_at TIMESTAMPTZ NOT NULL" in claims_ddl
        assert "attempt_n INT NOT NULL DEFAULT 1" in claims_ddl
        # CRITICAL: strategy_id is part of the PK
        assert "strategy_id TEXT NOT NULL" in claims_ddl
        assert (
            "PRIMARY KEY (asset, window_ts, timeframe, strategy_id)" in claims_ddl
        ), (
            "Audit #320 root-cause: without strategy_id in the PK, "
            "sibling strategies (v9_lgb_only, v9_ensemble, v10_lgb_only, …) "
            "fight for the SAME row on every eval tick."
        )

    def test_idempotent_migration_for_pre_320_installs(self, repo, conn):
        """Existing installs may have the pre-#320 PK without strategy_id.
        ensure() must add the column + drop+recreate the PK idempotently.
        """
        asyncio.run(repo.ensure_window_states_table())
        qs = [q for q, _ in conn.execute_calls]
        # Adds the column with IF NOT EXISTS for safe re-run
        assert any(
            "ADD COLUMN IF NOT EXISTS strategy_id" in q for q in qs
        ), "Migration must add strategy_id column for pre-#320 installs"
        # The DO $$ block guards on the existing PK shape so re-running
        # ensure() on a post-#320 install is a no-op.
        assert any(
            "DROP CONSTRAINT" in q and "ADD PRIMARY KEY" in q for q in qs
        ), "Migration must drop+re-create PK to include strategy_id"


# ── End-to-end dedup invariants (the real protection target) ────────────────


class TestDedupInvariants:
    """The whole point of this design — verify no double-fill, no lock-out."""

    def test_two_concurrent_claims_same_strategy_one_wins(self, repo, conn):
        # Two acquire_lease calls back-to-back for SAME strategy. First
        # gets the lease; second sees the active row and returns None.
        # This protects against same-strategy double-fire when eval N+1
        # races eval N.
        conn.fetchrow_result = [
            {"claim_id": "11111111-aaaa-bbbb-cccc-111111111111", "attempt_n": 1},
            None,  # second call: ON CONFLICT WHERE expires_at < now() is false
        ]
        key = make_key()
        first = asyncio.run(repo.acquire_lease(key, strategy_id="v9_lgb_only"))
        second = asyncio.run(repo.acquire_lease(key, strategy_id="v9_lgb_only"))
        assert first is not None
        assert second is None

    def test_failed_attempt_releases_for_retry(self, repo, conn):
        # Attempt 1: claim acquired, FAK fails, release lease
        # Attempt 2: claim acquired again (window now free for same strategy)
        conn.fetchrow_result = [
            {"claim_id": "11111111-aaaa-bbbb-cccc-111111111111", "attempt_n": 1},
            {"claim_id": "22222222-aaaa-bbbb-cccc-222222222222", "attempt_n": 1},
        ]
        key = make_key()
        first_claim = asyncio.run(
            repo.acquire_lease(key, strategy_id="v9_lgb_only")
        )
        asyncio.run(repo.release_lease(key, first_claim))
        second_claim = asyncio.run(
            repo.acquire_lease(key, strategy_id="v9_lgb_only")
        )
        assert first_claim and second_claim
        assert first_claim != second_claim

    def test_expired_lease_stolen_with_new_attempt_n(self, repo, conn):
        # Initial claim succeeds, then "process dies" (we don't release).
        # Next acquire should steal — attempt_n=2 indicates takeover.
        conn.fetchrow_result = [
            {"claim_id": "33333333-aaaa-bbbb-cccc-333333333333", "attempt_n": 2}
        ]
        key = make_key()
        claim_id = asyncio.run(
            repo.acquire_lease(
                key, strategy_id="v9_lgb_only", claimed_by="successor"
            )
        )
        assert claim_id == "33333333-aaaa-bbbb-cccc-333333333333"
        # The presence of attempt_n>1 in the result is what triggers the
        # "steal" log line in the code — verifies lease takeover semantics.


# ── 3-strategy race regression (audit #320 reproducer) ──────────────────────


class TestThreeStrategyRace:
    """Audit #320 reproducer (2026-04-26): three sibling strategies fire
    on the same window in the same eval tick. Pre-fix they all fought
    for the SAME row (per-window PK) and the first to acquire blocked
    the rest for 15s. Smoking-gun pattern in production:
        - window_claims.attempt_n=12 on one window
        - 11 consecutive ``release_lease rows_deleted=0`` logs
        - 0 trades in 6 hours

    Post-fix (per-strategy PK):
        - Each strategy has its OWN row (PK includes strategy_id)
        - All three acquire successfully on the same eval tick
        - One stuck holder (e.g. v10_lgb_only mid-FAK) does NOT block
          v9_lgb_only or v9_ensemble — they have separate rows.
    """

    def test_three_siblings_all_acquire_independently(self, repo, conn):
        """v9_lgb_only, v9_ensemble, v10_lgb_only on the same window:
        each gets its own row + claim_id, no contention."""
        # Each call is a fresh INSERT (different strategy_id → different
        # row → no ON CONFLICT). Three RETURNING rows scripted.
        conn.fetchrow_result = [
            {"claim_id": "aaaa1111-aaaa-bbbb-cccc-111111111111", "attempt_n": 1},
            {"claim_id": "bbbb2222-aaaa-bbbb-cccc-222222222222", "attempt_n": 1},
            {"claim_id": "cccc3333-aaaa-bbbb-cccc-333333333333", "attempt_n": 1},
        ]
        key = make_key(window_ts=1777223400)
        c1 = asyncio.run(repo.acquire_lease(key, strategy_id="v9_lgb_only"))
        c2 = asyncio.run(repo.acquire_lease(key, strategy_id="v9_ensemble"))
        c3 = asyncio.run(repo.acquire_lease(key, strategy_id="v10_lgb_only"))
        # All three got independent leases
        assert c1 == "aaaa1111-aaaa-bbbb-cccc-111111111111"
        assert c2 == "bbbb2222-aaaa-bbbb-cccc-222222222222"
        assert c3 == "cccc3333-aaaa-bbbb-cccc-333333333333"
        assert c1 != c2 != c3
        # Each call passed its own strategy_id to the SQL — verifies the
        # row keys are distinct per strategy.
        sids_passed = []
        for q, a in conn.execute_calls:
            if "INSERT INTO window_claims" in q:
                sids_passed.append(a[3])  # positional arg 4 (1-indexed)
        assert sids_passed == ["v9_lgb_only", "v9_ensemble", "v10_lgb_only"]

    def test_one_stuck_holder_does_not_block_siblings(self, repo, conn):
        """v9_lgb_only acquires + holds (mid-FAK, 15s lease). On the
        next eval tick (2s later), v10_lgb_only and v9_ensemble fire.
        With per-strategy PK, both v10 and v9_ensemble succeed even
        though v9_lgb_only's lease is still active.

        Pre-#320 this scenario produced 2 dedup_hits per eval cycle for
        15 seconds straight — siblings were starved by the lock holder.
        """
        # v9_lgb_only acquires: row inserted, claim_id_A returned.
        # v10_lgb_only acquires: separate row, claim_id_X returned.
        # v9_ensemble acquires: separate row, claim_id_Y returned.
        conn.fetchrow_result = [
            {"claim_id": "11111111-aaaa-bbbb-cccc-111111111111", "attempt_n": 1},
            {"claim_id": "99999999-aaaa-bbbb-cccc-999999999999", "attempt_n": 1},
            {"claim_id": "88888888-aaaa-bbbb-cccc-888888888888", "attempt_n": 1},
        ]
        key = make_key(window_ts=1777223400)
        # Tick 1: v9_lgb_only takes its lease (and is now mid-FAK)
        held = asyncio.run(
            repo.acquire_lease(key, strategy_id="v9_lgb_only")
        )
        assert held is not None
        # Tick 2 (2s later): v9 still holding. Siblings fire and must NOT
        # be blocked — they have their own rows.
        v10 = asyncio.run(
            repo.acquire_lease(key, strategy_id="v10_lgb_only")
        )
        ens = asyncio.run(
            repo.acquire_lease(key, strategy_id="v9_ensemble")
        )
        assert v10 is not None, (
            "v10_lgb_only must not be blocked by v9_lgb_only's active "
            "lease — that's the audit #320 root-cause bug."
        )
        assert ens is not None, (
            "v9_ensemble must not be blocked by v9_lgb_only's active "
            "lease — that's the audit #320 root-cause bug."
        )

    def test_same_strategy_re_eval_blocked_while_in_flight(self, repo, conn):
        """Within the SAME strategy: eval N+1 firing 2s after eval N
        (which is still mid-FAK) MUST be blocked. This is the original
        dedup invariant — same strategy must not double-fire on a window.

        Pre-#320 this worked accidentally because ALL sibling strategies
        also blocked each other (the bug). Post-#320 the per-strategy PK
        gives sibling strategies independence; this test pins the
        same-strategy block so we don't regress that protection.
        """
        # Tick N: v9 acquires.
        # Tick N+1: v9 acquires again (still mid-FAK from tick N) → ON
        # CONFLICT WHERE expires_at < now is false → RETURNING empty.
        conn.fetchrow_result = [
            {"claim_id": "aaaaaaaa-aaaa-bbbb-cccc-111111111111", "attempt_n": 1},
            None,
        ]
        key = make_key(window_ts=1777223400)
        first = asyncio.run(repo.acquire_lease(key, strategy_id="v9_lgb_only"))
        second = asyncio.run(repo.acquire_lease(key, strategy_id="v9_lgb_only"))
        assert first is not None
        assert second is None, (
            "Same strategy re-acquiring while its lease is still active "
            "MUST be blocked — preserving the dedup invariant."
        )

    def test_explicit_claim_id_threading_no_shim_clobber(self, repo, conn):
        """Audit #320 reproducer: pre-fix the in-memory shim
        ``_legacy_claim_ids`` was keyed by (asset, window_ts, timeframe)
        and shared across strategies. Sibling A's claim_id was clobbered
        by sibling B's call, then A's clear_trade_claim released the
        WRONG lease.

        Post-fix the API surfaces claim_id explicitly — siblings cannot
        clobber each other because there is no shared side-state. This
        test verifies that release uses the EXACT claim_id passed in,
        regardless of any other concurrent activity.
        """
        # Three siblings each acquire their own lease.
        conn.fetchrow_result = [
            {"claim_id": "aaaa1111-aaaa-bbbb-cccc-111111111111", "attempt_n": 1},
            {"claim_id": "bbbb2222-aaaa-bbbb-cccc-222222222222", "attempt_n": 1},
            {"claim_id": "cccc3333-aaaa-bbbb-cccc-333333333333", "attempt_n": 1},
        ]
        key = make_key(window_ts=1777223400)
        ok_a, claim_a = asyncio.run(
            repo.try_claim_trade(key, strategy_id="v9_lgb_only")
        )
        ok_b, claim_b = asyncio.run(
            repo.try_claim_trade(key, strategy_id="v9_ensemble")
        )
        ok_c, claim_c = asyncio.run(
            repo.try_claim_trade(key, strategy_id="v10_lgb_only")
        )
        assert ok_a and ok_b and ok_c
        assert claim_a != claim_b != claim_c

        # Now sibling A releases — must use ONLY claim_a, never b/c.
        asyncio.run(repo.clear_trade_claim(key, claim_a))
        delete_calls = [
            c for c in conn.execute_calls if "DELETE FROM window_claims" in c[0]
        ]
        assert len(delete_calls) == 1
        # The DELETE filters on claim_a, not claim_b or claim_c — proving
        # that explicit threading prevents the shim-clobber bug.
        assert delete_calls[0][1][3] == claim_a
        assert delete_calls[0][1][3] != claim_b
        assert delete_calls[0][1][3] != claim_c


# ── Integration with execute_trade.py call sites ────────────────────────────


class TestExecuteTradeIntegration:
    """Audits #316/#317/#320: confirm execute_trade.py uses the lease APIs
    correctly via per-strategy try_claim_trade / explicit claim_id
    threading to clear_trade_claim.

    These are static-source checks — full execute_trade integration is
    covered in the test_execute_trade_*.py suites.
    """

    def test_execute_trade_calls_try_claim_trade_with_strategy_id(self):
        from pathlib import Path

        path = Path(__file__).parent.parent / "use_cases" / "execute_trade.py"
        src = path.read_text()
        assert "try_claim_trade(" in src, (
            "execute_trade.py must call try_claim_trade — without it the "
            "lease is never acquired and dedup degenerates to was_traded "
            "fallback (which has no TTL guarantee on stale rows)."
        )
        # Audit #320: the call MUST pass strategy_id, otherwise sibling
        # strategies will silently collapse onto the same row again.
        assert "strategy_id=sid" in src or "strategy_id=" in src, (
            "execute_trade.py must pass strategy_id to try_claim_trade. "
            "Without it the lease key collapses across siblings (audit #320)."
        )

    def test_execute_trade_threads_claim_id_to_clear(self):
        """Audit #320 invariant: claim_id from try_claim_trade MUST be
        threaded explicitly into clear_trade_claim. The pre-#320 shim
        looked up the claim_id from a class-level dict, where sibling
        strategies overwrote each other, causing wrong releases.
        """
        from pathlib import Path

        path = Path(__file__).parent.parent / "use_cases" / "execute_trade.py"
        src = path.read_text()
        assert "clear_trade_claim(" in src, (
            "execute_trade.py must call clear_trade_claim on failure paths"
        )
        # Both clear_trade_claim call sites must pass claim_id explicitly.
        # We check that the closest preceding use case body has both
        # ``claim_id`` and the call.
        assert "clear_trade_claim(\n                        window_key, claim_id" in src or \
               "clear_trade_claim(window_key, claim_id" in src, (
            "execute_trade.py must thread claim_id into clear_trade_claim. "
            "Without it the release SQL targets the wrong row (audit #320)."
        )

    def test_execute_trade_runs_timing_recheck_before_claim(self):
        """ARCHITECTURAL INVARIANT (audit #317 root-cause, 2026-04-26):
        the wall-clock past-close guard MUST run BEFORE try_claim_trade.

        Earlier ordering acquired the lease first, then aborted on
        past-close and tried to release. Any non-deterministic release
        (claim_id mismatch, transient DB hiccup, shim race) would
        poison the dedup state — the row would linger for the full
        15s TTL, and every subsequent eval inside that TTL would hit
        dedup_hit. Running the timing recheck first eliminates the
        entire failure mode: the lease is never even touched on a
        stale-surface attempt.
        """
        from pathlib import Path

        path = Path(__file__).parent.parent / "use_cases" / "execute_trade.py"
        src = path.read_text()
        execute_idx = src.find("async def execute(\n")
        assert execute_idx > 0, "Could not locate execute() method"
        post_execute = src[execute_idx:]
        timing_call_idx = post_execute.find("_recheck_timing_before_execute(")
        claim_call_idx = post_execute.find("try_claim_trade(")
        assert timing_call_idx > 0, (
            "execute_trade.py must call _recheck_timing_before_execute()"
        )
        assert claim_call_idx > 0, (
            "execute_trade.py must call try_claim_trade()"
        )
        assert timing_call_idx < claim_call_idx, (
            "ARCHITECTURAL BUG: timing recheck must run BEFORE "
            "try_claim_trade. See audit #317 — running it after meant a "
            "lease was acquired and (sometimes) failed to release on "
            "stale-surface aborts, poisoning dedup state for 15s every "
            "time, multiplied across hundreds of past-close evals."
        )


# ── Crash-recovery semantics (audit #316) ───────────────────────────────────


class TestCrashRecovery:
    """Audit #316 reproducer: process holds a claim, dies before clear_trade_claim
    runs, lease must expire and be stealable by the successor without
    operator intervention.
    """

    def test_lease_steal_uses_increased_attempt_n(self, repo, conn):
        """Successor's claim_id is fresh AND attempt_n > 1 — proves it
        was a takeover, not a no-op."""
        conn.fetchrow_result = [
            {"claim_id": "55555555-aaaa-bbbb-cccc-555555555555", "attempt_n": 2}
        ]
        key = make_key()
        claim_id = asyncio.run(
            repo.acquire_lease(
                key, strategy_id="v9_lgb_only", claimed_by="recovered"
            )
        )
        assert claim_id == "55555555-aaaa-bbbb-cccc-555555555555"

    def test_release_with_wrong_claim_id_does_not_clear(self, repo, conn):
        """If process A holds the lease (claim_id_A) and process B
        mistakenly tries to release it with B's (wrong) claim_id, the
        SQL DELETE WHERE claim_id = $4 silently no-ops — A's lease
        survives.

        Post-#320 this is unambiguous: claim_id is threaded explicitly,
        so a wrong claim_id is purely a caller bug, not a shim race.
        """
        from domain.value_objects import WindowKey

        wrong_id = "99999999-aaaa-bbbb-cccc-999999999999"
        key = WindowKey(asset="BTC", window_ts=1_777_200_000, timeframe="5m")
        asyncio.run(repo.release_lease(key, wrong_id))
        # The DELETE was attempted with wrong_id — DB returns 0 rows
        # affected, but no exception is raised. Verify by checking the
        # exact query payload.
        delete_calls = [
            c for c in conn.execute_calls if "DELETE FROM window_claims" in c[0]
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0][1][3] == wrong_id


# ── Audit #317 reopen: timing-recheck-before-acquire (2026-04-26) ────────────


class TestTimingRecheckOrdering:
    """Regression net for the second occurrence of audit #317.

    Pre-fix: execute_trade acquired the lease THEN ran the wall-clock
    past-close guard. On past-close evals the lease was held and
    (sometimes) not released, leading to 200+ dedup_hits per closed
    window with zero fills until natural TTL expiry.

    Post-fix: timing recheck runs FIRST. Past-close evals never touch
    the lease, dedup state is never poisoned by stale-surface aborts.
    """

    def test_past_close_eval_does_not_acquire_lease(self):
        """Stale-surface (past-close) attempts must short-circuit BEFORE
        any DB lease activity. If we acquire then try to release, any
        release-path bug poisons dedup for the next 15s.
        """
        import asyncio

        from use_cases.execute_trade import ExecuteTradeUseCase
        from domain.value_objects import WindowMarket, StrategyDecision

        class FakeWindowState:
            def __init__(self):
                self.try_claim_calls = 0
                self.last_strategy_id = None

            async def try_claim_trade(self, key, *, strategy_id):
                self.try_claim_calls += 1
                self.last_strategy_id = strategy_id
                # Would acquire if asked — return new-API tuple shape.
                return (True, "fake-claim-id")

            async def clear_trade_claim(self, key, claim_id=None):
                pass

            async def was_traded(self, key):
                return False

            async def mark_traded(self, key, order_id):
                pass

        class FakeClock:
            def __init__(self, now):
                self._now = now

            def now(self):
                return self._now

        class FakeAlerter:
            async def send_system_alert(self, msg):
                pass

        # Window closed 5 minutes ago (1000 - 300 = 700, now=1300, offset=-300)
        window_ts = 700
        market = WindowMarket(
            condition_id="test",
            up_token_id="up",
            down_token_id="down",
            market_slug=f"btc-updown-5m-{window_ts}",
        )
        decision = StrategyDecision.trade(
            direction="UP",
            strategy_id="v9_lgb_only",
            strategy_version="9.1.0-lgb",
            entry_reason="test",
            confidence="HIGH",
            confidence_score=0.9,
            entry_cap=0.50,
            collateral_pct=0.025,
        )

        ws = FakeWindowState()
        uc = ExecuteTradeUseCase(
            polymarket=None,
            order_executor=None,
            risk_manager=None,
            window_state=ws,
            alerter=FakeAlerter(),
            trade_recorder=None,
            clock=FakeClock(now=1300.0),  # well past close at 1000
            paper_mode=False,
        )
        result = asyncio.run(
            uc.execute(
                decision=decision,
                window_market=market,
                current_btc_price=78000.0,
                open_price=78000.0,
            )
        )
        # The trade must abort cleanly with eval_offset_past_close —
        # AND the lease must never be touched.
        assert not result.success
        assert "past_close" in (result.failure_reason or "")
        assert ws.try_claim_calls == 0, (
            "Past-close timing recheck must short-circuit BEFORE "
            "try_claim_trade. Acquiring + releasing on every past-close "
            "eval is what poisoned dedup state in audit #317."
        )

    def test_fak_failure_releases_lease_for_retry(self, repo, conn):
        """On a real (in-window) FAK no-fill, the claim MUST be released
        so the next eval offset can retry within the same window. This
        is the original audit #316/#317 invariant.

        Post-#320 the caller threads claim_id explicitly — no more shim
        side-state to clobber.
        """
        # Acquire the lease via try_claim_trade
        conn.fetchrow_result = [
            {"claim_id": "abcd1234-aaaa-bbbb-cccc-1234567890ab", "attempt_n": 1}
        ]
        key = make_key()
        ok, claim_id = asyncio.run(
            repo.try_claim_trade(key, strategy_id="v9_lgb_only")
        )
        assert ok is True
        assert claim_id == "abcd1234-aaaa-bbbb-cccc-1234567890ab"

        # Caller (execute_trade) detects FAK no-fill, calls clear_trade_claim
        # with the EXPLICIT claim_id from try_claim_trade.
        asyncio.run(repo.clear_trade_claim(key, claim_id))

        # The DELETE statement was issued with the correct claim_id
        delete_calls = [
            c for c in conn.execute_calls if "DELETE FROM window_claims" in c[0]
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0][1][3] == "abcd1234-aaaa-bbbb-cccc-1234567890ab"

    def test_clear_trade_claim_warns_when_no_claim_id(self, repo, conn, caplog):
        """If clear_trade_claim is invoked without a claim_id (caller
        bug), it must surface a warning. Pre-#320 this was a silent
        no-op via the shim — masked the very state-machine bugs we
        were trying to fix."""
        import logging

        caplog.set_level(logging.WARNING)
        key = make_key()
        # Caller forgot to thread claim_id
        asyncio.run(repo.clear_trade_claim(key, None))
        # No DB activity should occur
        assert len(conn.execute_calls) == 0

    def test_release_lease_logs_rows_deleted(self, repo, conn):
        """release_lease must surface DELETE row count at INFO level so
        ops can verify a release actually landed (vs. silently no-opping
        on a claim_id mismatch). Pre-fix: DEBUG only, masked in prod."""
        import asyncio

        # Stub asyncpg's command-tag string return for execute()
        async def _execute_returning_tag(q, *a):
            conn.execute_calls.append((q, a))
            return "DELETE 1"

        conn.execute = _execute_returning_tag
        key = make_key()
        asyncio.run(
            repo.release_lease(key, "abcd1234-aaaa-bbbb-cccc-1234567890ab")
        )
        # DELETE was attempted
        delete_calls = [
            c for c in conn.execute_calls if "DELETE FROM window_claims" in c[0]
        ]
        assert len(delete_calls) == 1
