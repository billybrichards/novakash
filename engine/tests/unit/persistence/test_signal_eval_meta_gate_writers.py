"""Writer-regression tests for the three meta-gate columns:
  signal_evaluations.probability_v2_meta_gate
  signal_evaluations.probability_v9_2_meta_gate
  signal_evaluations.probability_v12_meta_gate

(this PR — feat/meta-gate-signal-eval-writers, 2026-05-25.)

Mirrors test_signal_eval_lgb_btc_pure_writers.py structure. Covers:
- DBClient.update_signal_evaluations_v2_meta_gate / v9_2_meta_gate /
  v12_meta_gate upserts correctly per column
- No-op when probability is None / eval_offset is None / no pool
- Swallows DB errors gracefully (fire-and-forget contract, uses
  exc_log_fields per PR #600 to keep errors visible in logs)
- PgSignalRepository parity: each writer method exists

Audit #963 (2026-05-12) added the three columns via
migrations/add_meta_gate_scores.sql. These sidecar writers close the
per-row gap on signal_evaluations (bulk path on window_snapshots
already populated via pg_signal_repo lines 67/127/388/690).
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


# ── v2 meta-gate writer ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_meta_gate_writer_upserts_row():
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_v2_meta_gate(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_v2_meta_gate=0.83,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "probability_v2_meta_gate" in sql
    assert "probability_v2_meta_gate = COALESCE" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    args = call["args"]
    assert args[0] == 1777617300
    assert args[1] == "BTC"
    assert args[2] == "5m"
    assert args[3] == 120
    assert args[4] == pytest.approx(0.83)


@pytest.mark.asyncio
async def test_v2_meta_gate_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_v2_meta_gate(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_v2_meta_gate=None,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_v2_meta_gate_writer_noop_when_eval_offset_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_v2_meta_gate(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=None,
        probability_v2_meta_gate=0.83,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_v2_meta_gate_writer_swallows_db_error():
    db = _failing_db()
    # Must NOT raise.
    n = await db.update_signal_evaluations_v2_meta_gate(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_v2_meta_gate=0.83,
    )
    assert n == 0


# ── v9.2 meta-gate writer ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v9_2_meta_gate_writer_upserts_row():
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_v9_2_meta_gate(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_v9_2_meta_gate=0.77,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "probability_v9_2_meta_gate" in sql
    # Must NOT touch the v2 or v12 columns.
    assert "probability_v2_meta_gate =" not in sql
    assert "probability_v12_meta_gate =" not in sql
    args = call["args"]
    assert args[4] == pytest.approx(0.77)


@pytest.mark.asyncio
async def test_v9_2_meta_gate_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_v9_2_meta_gate(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_v9_2_meta_gate=None,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_v9_2_meta_gate_writer_swallows_db_error():
    db = _failing_db()
    n = await db.update_signal_evaluations_v9_2_meta_gate(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_v9_2_meta_gate=0.77,
    )
    assert n == 0


# ── v12 meta-gate writer ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v12_meta_gate_writer_upserts_row():
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_v12_meta_gate(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_v12_meta_gate=0.65,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "probability_v12_meta_gate" in sql
    # Must NOT touch the v2 or v9_2 columns.
    assert "probability_v2_meta_gate =" not in sql
    assert "probability_v9_2_meta_gate =" not in sql
    args = call["args"]
    assert args[4] == pytest.approx(0.65)


@pytest.mark.asyncio
async def test_v12_meta_gate_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_v12_meta_gate(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_v12_meta_gate=None,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_v12_meta_gate_writer_swallows_db_error():
    db = _failing_db()
    n = await db.update_signal_evaluations_v12_meta_gate(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_v12_meta_gate=0.65,
    )
    assert n == 0


# ── PgSignalRepository parity ─────────────────────────────────────────────


def test_pg_signal_repo_has_all_three_meta_gate_writers():
    """The PgSignalRepository must mirror DBClient method-for-method (lesson
    from PR #439 — keep the two writers verbatim). Verify each meta-gate
    writer exists as an async method on the repo class."""
    from adapters.persistence.pg_signal_repo import PgSignalRepository

    assert hasattr(PgSignalRepository, "update_signal_evaluations_v2_meta_gate")
    assert hasattr(PgSignalRepository, "update_signal_evaluations_v9_2_meta_gate")
    assert hasattr(PgSignalRepository, "update_signal_evaluations_v12_meta_gate")
