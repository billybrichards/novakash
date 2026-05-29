"""Regression tests for the dedup_key-based hard lock in
PgTradeRepository.has_fill_for_strategy_window_direction.

Driver incident: tickformer_v16_pure placed TWO real Polymarket orders on
window_ts=1780091700 direction=UP at 2026-05-29 21:58:01 and 21:58:35 UTC
(trades 9442 + 9443, -$10 instead of -$5).

Root cause:
  1. _tickformer_base.py did not set metadata['window_ts'] on decisions.
  2. has_fill_for_strategy_window_direction queried
     COALESCE(metadata->>'window_ts', '') which collapsed to ''
     (never matching the real epoch), so the hard lock returned False
     and allowed the second concurrent evaluate_all to fire a second order.

Two-layer fix:
  Layer 1a: _tickformer_base.py now sets meta['window_ts'] = window_ts.
  Layer 1b: has_fill_for_strategy_window_direction now checks BOTH:
    Clause A — metadata->>'window_ts' (canonical path)
    Clause B — metadata->>'dedup_key' (fallback for strategies without window_ts)
  Layer 2: UNIQUE partial index on metadata->>'dedup_key' (migration
    add_trades_dedup_key_unique_index.sql).

These tests exercise the Clause B path and the Layer 1a metadata fix.
They use a fake asyncpg pool — no I/O required.
"""
from __future__ import annotations

from typing import Any

import pytest

from adapters.persistence.pg_trade_repo import PgTradeRepository


# ── Fake asyncpg helpers ──────────────────────────────────────────────


class _FakeConn:
    """Records SQL + params that asyncpg would run. Returns canned results."""

    def __init__(self, fetchrow_returns=None):
        self._fetchrow_returns = fetchrow_returns  # None → no row found
        self.fetchrow_calls: list[dict[str, Any]] = []
        self._raise_on_fetchrow: Exception | None = None

    async def fetchrow(self, sql, *args, **kwargs):
        if self._raise_on_fetchrow is not None:
            raise self._raise_on_fetchrow
        self.fetchrow_calls.append({"sql": sql, "args": args})
        return self._fetchrow_returns


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer._conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _repo(fetchrow_returns=None) -> tuple[PgTradeRepository, _FakeConn]:
    conn = _FakeConn(fetchrow_returns=fetchrow_returns)
    repo = PgTradeRepository(_FakePool(conn))
    return repo, conn


def _repo_that_raises(exc: Exception) -> tuple[PgTradeRepository, _FakeConn]:
    conn = _FakeConn()
    conn._raise_on_fetchrow = exc
    repo = PgTradeRepository(_FakePool(conn))
    return repo, conn


# ── Tests: Clause A + Clause B query structure ────────────────────────


@pytest.mark.asyncio
async def test_has_fill_returns_false_when_no_row():
    """No trade in DB → returns False → trade is allowed."""
    repo, conn = _repo(fetchrow_returns=None)
    result = await repo.has_fill_for_strategy_window_direction(
        strategy_id="tickformer_v16_pure",
        window_ts=1780091700,
        direction="UP",
        timeframe="5m",
        asset="BTC",
        is_live=True,
    )
    assert result is False
    assert len(conn.fetchrow_calls) == 1


@pytest.mark.asyncio
async def test_has_fill_returns_true_when_row_found():
    """Existing trade row in DB → returns True → blocks duplicate order."""
    repo, conn = _repo(fetchrow_returns={"1": 1})  # any truthy row
    result = await repo.has_fill_for_strategy_window_direction(
        strategy_id="tickformer_v16_pure",
        window_ts=1780091700,
        direction="UP",
        timeframe="5m",
        asset="BTC",
        is_live=True,
    )
    assert result is True


@pytest.mark.asyncio
async def test_dedup_key_included_in_query():
    """Clause B: the query must include the dedup_key parameter so a
    tickformer trade with dedup_key but no window_ts is still detected.

    Regression guard: before the fix, the query had no $7 parameter and
    the dedup_key path was absent. This test MUST fail if the Clause B
    path is removed from has_fill_for_strategy_window_direction.
    """
    repo, conn = _repo(fetchrow_returns=None)
    await repo.has_fill_for_strategy_window_direction(
        strategy_id="tickformer_v16_pure",
        window_ts=1780091700,
        direction="UP",
        timeframe="5m",
        asset="BTC",
        is_live=True,
    )
    assert len(conn.fetchrow_calls) == 1
    call = conn.fetchrow_calls[0]
    sql = call["sql"]
    args = call["args"]

    # The query must contain the dedup_key lookup
    assert "dedup_key" in sql, (
        "Clause B (dedup_key lookup) is missing from the SQL. "
        "Regression: the fix for the 2026-05-29 tickformer double-fire "
        "incident requires this path."
    )
    # The expected dedup_key must be one of the positional args
    expected_dedup_key = "tickformer_v16_pure:1780091700:UP"
    assert expected_dedup_key in args, (
        f"Expected dedup_key '{expected_dedup_key}' in query args {args}"
    )


@pytest.mark.asyncio
async def test_dedup_key_format_encodes_strategy_window_direction():
    """The dedup_key passed to the query must match the format produced by
    trade_recorder.py and _tickformer_base.py:
    '{strategy_id}:{window_ts}:{direction}'.
    """
    repo, conn = _repo(fetchrow_returns=None)
    await repo.has_fill_for_strategy_window_direction(
        strategy_id="v9_2_eth_raw_lgb",
        window_ts=1779312000,
        direction="DOWN",
        timeframe="5m",
        asset="ETH",
        is_live=True,
    )
    args = conn.fetchrow_calls[0]["args"]
    expected = "v9_2_eth_raw_lgb:1779312000:DOWN"
    assert expected in args, f"Expected '{expected}' in args, got {args}"


@pytest.mark.asyncio
async def test_cancelled_trade_filtered_by_status_where_clause():
    """The query's WHERE clause must filter CANCELLED/SKIPPED/FAILED_EXECUTION.
    When all matching rows carry those statuses the DB returns no row.
    has_fill_for_strategy_window_direction must return False in that case
    so the order is allowed (the previous attempt was cancelled, not filled).
    """
    # Simulate DB returning no row (as it would when all matching rows are
    # CANCELLED/SKIPPED/FAILED_EXECUTION due to the WHERE filter).
    repo, conn = _repo(fetchrow_returns=None)
    result = await repo.has_fill_for_strategy_window_direction(
        strategy_id="tickformer_v16_pure",
        window_ts=1780091700,
        direction="UP",
        timeframe="5m",
        asset="BTC",
        is_live=True,
    )
    assert result is False, (
        "A cancelled/skipped trade must not block future orders. "
        "The DB-side WHERE filter should return no row for cancelled trades."
    )
    # Validate the SQL contains all three status exclusions
    sql = conn.fetchrow_calls[0]["sql"]
    assert "CANCELLED" in sql
    assert "SKIPPED" in sql
    assert "FAILED_EXECUTION" in sql


@pytest.mark.asyncio
async def test_fail_closed_on_db_error():
    """DB error during the query → returns True (fail-closed).
    Better to skip ONE legitimate fire than risk a repeat double-order.
    """
    repo, conn = _repo_that_raises(RuntimeError("simulated pool exhaustion"))
    result = await repo.has_fill_for_strategy_window_direction(
        strategy_id="tickformer_v16_pure",
        window_ts=1780091700,
        direction="UP",
        timeframe="5m",
        asset="BTC",
        is_live=True,
    )
    assert result is True, (
        "DB error must fail-closed (return True = block the order). "
        "Regression: 2026-05-29 tickformer incident cost $10; fail-open "
        "on DB errors is the same risk vector."
    )


# ── Tests: Layer 1a — tickformer metadata fix ─────────────────────────


def test_tickformer_base_sets_window_ts_in_meta():
    """_tickformer_base.py must include 'window_ts' in the TRADE decision
    metadata.

    Regression guard: before the fix, window_ts was read from the surface
    for internal use (_bump_and_check) but never added to meta. The hard
    lock's Clause A path (COALESCE(metadata->>'window_ts', '')) would
    produce '' != '1780091700' and fail to detect the existing trade.

    This test MUST fail if the window_ts assignment is removed from
    _tickformer_base.py.
    """
    import os
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

    from strategies import gate_params as _gp
    from strategies.configs import _tickformer_base

    # Reset consec state to avoid test pollution.
    _tickformer_base._consec_state.clear()

    surface = SimpleNamespace(
        asset="BTC",
        window_ts=1780091700,
        eval_offset=120,
        tickformer_trade_signal="HOLD",
        clob_implied_up=0.70,
        fill_price=0.70,
        probability_tickformer_v16=0.90,  # above threshold
    )

    params = {
        "up_threshold": 0.85,
        "down_threshold": 0.15,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 240,
        "shadow_only": 0,
        "min_consecutive_pass_ticks": 1,
        "entry_cap": 0.93,
        "entry_floor_up": 0.60,
    }
    token = _gp.set_active(params)
    try:
        decision = _tickformer_base.evaluate_tickformer_strategy(
            surface,
            prob_column="probability_tickformer_v16",
            strategy_id="tickformer_v16_pure",
            version="1.0.0",
            default_up_threshold=0.85,
            default_rem_min=0,
            default_rem_max=240,
        )
    finally:
        _gp.reset_active(token)

    assert decision.action == "TRADE", (
        f"Expected TRADE decision, got {decision.action} "
        f"(skip_reason={decision.skip_reason})"
    )
    assert "window_ts" in (decision.metadata or {}), (
        "metadata['window_ts'] is missing from the tickformer TRADE decision. "
        "This is the root cause of the 2026-05-29 double-fire incident: "
        "has_fill_for_strategy_window_direction (Clause A) could not find "
        "the existing trade because COALESCE(metadata->>'window_ts', '') "
        "returned '' (NULL collapsed to empty string)."
    )
    assert decision.metadata["window_ts"] == surface.window_ts, (
        f"metadata['window_ts']={decision.metadata.get('window_ts')} != "
        f"surface.window_ts={surface.window_ts}"
    )


def test_tickformer_base_window_ts_present_on_skip_path():
    """window_ts should also be present in SKIP decision metadata for
    consistency, so any future consumer of skip metadata can correlate
    to the window correctly.
    """
    import os
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

    from strategies import gate_params as _gp
    from strategies.configs import _tickformer_base

    _tickformer_base._consec_state.clear()

    surface = SimpleNamespace(
        asset="BTC",
        window_ts=1780091700,
        eval_offset=120,
        tickformer_trade_signal=None,
        clob_implied_up=0.70,
        fill_price=0.70,
        probability_tickformer_v16=0.50,  # below threshold → SKIP
    )

    params = {
        "up_threshold": 0.85,
        "down_threshold": 0.15,
        "eval_offset_remaining_min": 0,
        "eval_offset_remaining_max": 240,
        "shadow_only": 0,
        "min_consecutive_pass_ticks": 1,
        "entry_cap": 0.93,
        "entry_floor_up": 0.60,
    }
    token = _gp.set_active(params)
    try:
        decision = _tickformer_base.evaluate_tickformer_strategy(
            surface,
            prob_column="probability_tickformer_v16",
            strategy_id="tickformer_v16_pure",
            version="1.0.0",
            default_up_threshold=0.85,
            default_rem_min=0,
            default_rem_max=240,
        )
    finally:
        _gp.reset_active(token)

    assert decision.action == "SKIP"
    assert "window_ts" in (decision.metadata or {}), (
        "window_ts should be in SKIP decision metadata too (consistency)."
    )


# ── Documented design constraint (not unit-testable without I/O) ──────


def test_concurrent_race_design_note():
    """DOCUMENT (not execute) the concurrent race scenario fixed by this PR.

    The application-level race between two concurrent evaluate_all calls
    (one from the normal CLOSING window path, one from the
    _on_tickformer_snapshot create_task path) is caught by a combination of:

      1. try_claim_fill_slot (strategy_window_fills UNIQUE constraint):
         only ONE concurrent execute_uc attempt wins the DB INSERT.
         The second attempt returns slot_claimed=False → skips FAK.

      2. has_fill_for_strategy_window_direction (Step -0.5):
         after the first trade row commits, Clause A (window_ts) OR
         Clause B (dedup_key) will return True for any retry that
         cleared the fill slot (e.g. stale placeholder after 25s TTL).

      3. Layer 2 UNIQUE index (idx_trades_dedup_key_unique):
         any insert that bypasses both application gates raises a
         PostgreSQL UniqueViolation before the trade row commits.

    This test passes trivially — it is a design-decision documentation
    placeholder. The concurrent race test lives in the integration suite.
    """
    pass  # Documented constraint; no assertions needed.
