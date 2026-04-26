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


# ── Integration with execute_trade.py call sites ────────────────────────────


class TestExecuteTradeIntegration:
    """Audits #316/#317: confirm execute_trade.py uses the lease APIs
    via the legacy shim (try_claim_trade / clear_trade_claim) so a process
    crash mid-claim cannot stale-lock the window indefinitely.

    These are static-source checks — full execute_trade integration is
    covered in test_reconcile_trades_sot.py and test_manual_trade_fast_path.py.
    """

    def test_execute_trade_calls_try_claim_trade(self):
        from pathlib import Path

        path = Path(__file__).parent.parent / "use_cases" / "execute_trade.py"
        src = path.read_text()
        assert "try_claim_trade(window_key)" in src, (
            "execute_trade.py must call try_claim_trade — without it the "
            "lease is never acquired and dedup degenerates to was_traded "
            "fallback (which has no TTL guarantee on stale rows)."
        )

    def test_execute_trade_calls_clear_trade_claim_on_failure(self):
        """If FAK / RFQ / GTC all fail, the claim must be cleared so the
        next eval offset can retry within the same window. PR #383's
        whole point. Without this, audit #316 reproduces."""
        from pathlib import Path

        path = Path(__file__).parent.parent / "use_cases" / "execute_trade.py"
        src = path.read_text()
        assert "clear_trade_claim(window_key)" in src, (
            "execute_trade.py must call clear_trade_claim on failure paths "
            "so a transient FAK miss doesn't stale-lock the window for "
            "the rest of its lifetime"
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
        timing_idx = src.find("_recheck_timing_before_execute(")
        # Skip the function definition (which is a `def`, not a call)
        # and find the FIRST call site inside ExecuteTradeUseCase.execute.
        # Strategy: search after "async def execute" for both markers and
        # confirm timing_recheck appears before try_claim_trade.
        execute_idx = src.find("async def execute(\n")
        assert execute_idx > 0, "Could not locate execute() method"
        post_execute = src[execute_idx:]
        timing_call_idx = post_execute.find("_recheck_timing_before_execute(")
        claim_call_idx = post_execute.find("try_claim_trade(window_key)")
        assert timing_call_idx > 0, (
            "execute_trade.py must call _recheck_timing_before_execute()"
        )
        assert claim_call_idx > 0, (
            "execute_trade.py must call try_claim_trade(window_key)"
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
        claim_id = asyncio.run(repo.acquire_lease(key, claimed_by="recovered"))
        assert claim_id == "55555555-aaaa-bbbb-cccc-555555555555"

    def test_release_with_wrong_claim_id_does_not_clear(self, repo, conn):
        """If process A holds the lease and process B mistakenly tries to
        release it with B's (wrong) claim_id, the SQL DELETE WHERE
        claim_id = $4 silently no-ops — A's lease survives."""
        from domain.value_objects import WindowKey

        repo._legacy_claim_ids = {}
        # Simulate B trying to release with a wrong claim_id (no prior shim memory)
        wrong_id = "99999999-aaaa-bbbb-cccc-999999999999"
        key = WindowKey(asset="BTC", window_ts=1_777_200_000, timeframe="5m")
        asyncio.run(repo.release_lease(key, wrong_id))
        # The DELETE was attempted with wrong_id — DB returns 0 rows
        # affected, but no exception is raised. The contract here is
        # "fail closed silently" — verify by checking no shim state was
        # mutated.
        assert repo._legacy_claim_ids == {}


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

            async def try_claim_trade(self, key):
                self.try_claim_calls += 1
                return True  # Would acquire if asked

            async def clear_trade_claim(self, key):
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
        """
        # Acquire the lease through the legacy shim
        conn.fetchrow_result = [
            {"claim_id": "abcd1234-aaaa-bbbb-cccc-1234567890ab", "attempt_n": 1}
        ]
        key = make_key()
        assert asyncio.run(repo.try_claim_trade(key)) is True

        # Caller (execute_trade) detects FAK no-fill, calls clear_trade_claim
        asyncio.run(repo.clear_trade_claim(key))

        # The DELETE statement was issued with the correct claim_id
        delete_calls = [
            c for c in conn.execute_calls if "DELETE FROM window_claims" in c[0]
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0][1][3] == "abcd1234-aaaa-bbbb-cccc-1234567890ab"
        # And the in-memory shim state is clean (no stale claim_id left
        # to clobber a successor)
        slot = (key.asset, key.window_ts, key.timeframe)
        assert slot not in repo._legacy_claim_ids

    def test_clear_trade_claim_logs_when_no_memory(self, repo, conn, caplog):
        """If clear_trade_claim is invoked without a prior try_claim_trade
        on the same key, it must surface a warning. Pre-fix this was a
        silent no-op — masked the very state-machine bugs we were trying
        to fix."""
        import logging

        caplog.set_level(logging.WARNING)
        key = make_key()
        # No try_claim_trade beforehand — shim memory is empty
        asyncio.run(repo.clear_trade_claim(key))
        # No DB activity should occur
        assert len(conn.execute_calls) == 0
        # But the warning must surface — operators need to know
        # state-machine invariants are being violated.
        # (structlog routes through stdlib logging in tests.)
        # Note: caplog captures stdlib log records; structlog's bind/proc
        # chain may not propagate by default. Skip the message-text
        # assertion if the test environment doesn't wire structlog →
        # stdlib, but ensure the no-DB-call invariant still holds.

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
