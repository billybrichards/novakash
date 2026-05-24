"""Writer-regression tests for the three BTC PURE columns:
  signal_evaluations.probability_lgb_v9_3_btc_pure
  signal_evaluations.probability_lgb_v9_2_pure
  signal_evaluations.probability_lgb_v12_pure

(this PR — feat/v9_3_btc_pure_lgb_strategy, 2026-05-24.)

Mirrors test_signal_eval_lgb_v9_3_btc_writer.py structure. Covers:
- DBClient.update_signal_evaluations_lgb_v9_3_btc_pure / v9_2_pure / v12_pure
  upserts correctly per column
- No-op when probability is None / eval_offset is None / no pool
- Swallows DB errors gracefully (fire-and-forget contract)
- PgSignalRepository parity: each writer method exists

Walk-forward CV: /tmp/btc_walkforward_results.md.
RDS notes #618, #631, #632 (blend bug discovery).
"""

from __future__ import annotations

from typing import Any

import pytest

from persistence.db_client import DBClient


# ── Shared test infrastructure ────────────────────────────────────────────


class _FakeConn:
    def __init__(self, result: str = "INSERT 0 1") -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    async def execute(self, sql: str, *args, **kwargs):
        self.calls.append({"sql": sql, "args": args, "kwargs": kwargs})
        return self._result


class _FakePool:
    def __init__(self, result: str = "INSERT 0 1") -> None:
        self.conn = _FakeConn(result)

    def acquire(self, **kwargs):
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _stub_db(result: str = "INSERT 0 1") -> DBClient:
    db = DBClient.__new__(DBClient)
    db._pool = _FakePool(result)
    return db


class _FailingConn:
    async def execute(self, sql: str, *args, **kwargs):
        raise RuntimeError("simulated DB failure")


class _FailingPool:
    def acquire(self, **kwargs):
        class _CM:
            async def __aenter__(self_inner):
                return _FailingConn()

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _failing_db() -> DBClient:
    db = DBClient.__new__(DBClient)
    db._pool = _FailingPool()
    return db


# ── v9.3 BTC PURE writer ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v9_3_btc_pure_writer_upserts_row():
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_lgb_v9_3_btc_pure(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_3_btc_pure=0.95,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "probability_lgb_v9_3_btc_pure" in sql
    # Critical: this column must be different from the (blended)
    # probability_lgb_v9_3_btc — different SQL statement.
    assert "probability_lgb_v9_3_btc_pure = COALESCE" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    args = call["args"]
    assert args[0] == 1777617300
    assert args[1] == "BTC"
    assert args[2] == "5m"
    assert args[3] == 120
    assert args[4] == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_v9_3_btc_pure_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_3_btc_pure(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_3_btc_pure=None,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_v9_3_btc_pure_writer_noop_when_eval_offset_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_3_btc_pure(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=None,
        probability_lgb_v9_3_btc_pure=0.95,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_v9_3_btc_pure_writer_swallows_db_error():
    db = _failing_db()
    # Must NOT raise.
    n = await db.update_signal_evaluations_lgb_v9_3_btc_pure(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_3_btc_pure=0.95,
    )
    assert n == 0


# ── v9.2 BTC PURE writer ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v9_2_pure_writer_upserts_row():
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_lgb_v9_2_pure(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_2_pure=0.94,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "probability_lgb_v9_2_pure" in sql
    # Must NOT touch the blended column.
    assert "probability_lgb_v9_2 =" not in sql
    args = call["args"]
    assert args[4] == pytest.approx(0.94)


@pytest.mark.asyncio
async def test_v9_2_pure_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_2_pure(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_2_pure=None,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_v9_2_pure_writer_swallows_db_error():
    db = _failing_db()
    n = await db.update_signal_evaluations_lgb_v9_2_pure(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_2_pure=0.94,
    )
    assert n == 0


# ── v12 BTC PURE writer ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v12_pure_writer_upserts_row():
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_lgb_v12_pure(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v12_pure=0.91,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "probability_lgb_v12_pure" in sql
    # Must NOT touch the blended column.
    assert "probability_lgb_v12 =" not in sql
    args = call["args"]
    assert args[4] == pytest.approx(0.91)


@pytest.mark.asyncio
async def test_v12_pure_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v12_pure(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v12_pure=None,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_v12_pure_writer_swallows_db_error():
    db = _failing_db()
    n = await db.update_signal_evaluations_lgb_v12_pure(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v12_pure=0.91,
    )
    assert n == 0


# ── PgSignalRepository parity ─────────────────────────────────────────────


def test_pg_signal_repo_has_all_three_pure_writers():
    """The PgSignalRepository must mirror DBClient method-for-method (lesson
    from PR #439 — keep the two writers verbatim). Verify each PURE writer
    exists as an async method on the repo class."""
    from adapters.persistence.pg_signal_repo import PgSignalRepository

    assert hasattr(PgSignalRepository, "update_signal_evaluations_lgb_v9_3_btc_pure")
    assert hasattr(PgSignalRepository, "update_signal_evaluations_lgb_v9_2_pure")
    assert hasattr(PgSignalRepository, "update_signal_evaluations_lgb_v12_pure")
