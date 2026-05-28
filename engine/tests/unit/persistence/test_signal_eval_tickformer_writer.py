"""Writer tests for DBClient.update_signal_evaluations_tickformer.

Pins the SQL contract for all six TickFormer signal_evaluations columns:
  - probability_tickformer_v16 / v17 / v18 / v20 (NUMERIC)
  - tickformer_gate_cond (NUMERIC)
  - tickformer_trade_signal (TEXT)

PR #622 added v16/v17/v18 probability columns but missed gate_cond and
trade_signal. This writer (added by fix/tickformer-strategies-actually-fire)
closes the gap and also adds v20 support.

Bug B in fix/tickformer-strategies-actually-fire.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from persistence.db_client import DBClient


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

    def acquire(self, *, timeout=None):
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


# ── Upsert contract ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tickformer_writer_upserts_all_six_columns():
    """Writer issues INSERT...ON CONFLICT with all six tickformer columns."""
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_tickformer(
        window_ts=1779000000,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_tickformer_v16=0.91,
        probability_tickformer_v17=0.88,
        probability_tickformer_v18=0.93,
        probability_tickformer_v20=0.87,
        tickformer_gate_cond=0.74,
        tickformer_trade_signal="UP",
    )
    assert n == 1
    assert len(db._pool.conn.calls) == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]

    # Basic SQL shape
    assert "INSERT INTO signal_evaluations" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "DO UPDATE SET" in sql

    # All six columns present in both INSERT and UPDATE sections
    for col in (
        "probability_tickformer_v16",
        "probability_tickformer_v17",
        "probability_tickformer_v18",
        "probability_tickformer_v20",
        "tickformer_gate_cond",
        "tickformer_trade_signal",
    ):
        assert col in sql, f"column {col!r} missing from SQL"

    # COALESCE semantics on numeric columns
    for col in (
        "probability_tickformer_v16",
        "probability_tickformer_v17",
        "probability_tickformer_v18",
        "probability_tickformer_v20",
        "tickformer_gate_cond",
        "tickformer_trade_signal",
    ):
        flat = sql.replace("\n", " ").replace("  ", " ")
        assert f"COALESCE(\n                            signal_evaluations.{col}" in sql or \
               f"COALESCE( signal_evaluations.{col}" in flat or \
               f"COALESCE(signal_evaluations.{col}" in flat.replace("  ", ""), \
               f"COALESCE not found for {col!r}"

    # Args: window_ts, asset, timeframe, eval_offset, v16, v17, v18, v20, gate_cond, signal
    args = call["args"]
    assert args[0] == 1779000000
    assert args[1] == "BTC"
    assert args[2] == "5m"
    assert args[3] == 120
    assert args[4] == 0.91   # v16
    assert args[5] == 0.88   # v17
    assert args[6] == 0.93   # v18
    assert args[7] == 0.87   # v20
    assert args[8] == 0.74   # gate_cond
    assert args[9] == "UP"   # trade_signal


@pytest.mark.asyncio
async def test_tickformer_writer_partial_columns_still_upserts():
    """Writer fires when only some columns are populated (e.g. v18 only)."""
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_tickformer(
        window_ts=1779000000,
        asset="ETH",
        timeframe="5m",
        eval_offset=180,
        probability_tickformer_v18=0.91,
        tickformer_trade_signal="HOLD",
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    args = call["args"]
    # v16, v17, v20, gate_cond should be None
    assert args[4] is None   # v16
    assert args[5] is None   # v17
    assert args[6] == 0.91   # v18
    assert args[7] is None   # v20
    assert args[8] is None   # gate_cond
    assert args[9] == "HOLD" # trade_signal


# ── Skip / no-op cases ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tickformer_writer_noop_when_eval_offset_none():
    """eval_offset is part of the unique key; None → no SQL issued."""
    db = _stub_db()
    n = await db.update_signal_evaluations_tickformer(
        window_ts=1779000000, asset="BTC", timeframe="5m",
        eval_offset=None, probability_tickformer_v18=0.92,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_tickformer_writer_noop_when_all_values_none():
    """If every column is None there's nothing to write."""
    db = _stub_db()
    n = await db.update_signal_evaluations_tickformer(
        window_ts=1779000000, asset="BTC", timeframe="5m",
        eval_offset=120,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_tickformer_writer_noop_when_no_pool():
    db = DBClient.__new__(DBClient)
    db._pool = None
    n = await db.update_signal_evaluations_tickformer(
        window_ts=1779000000, asset="BTC", timeframe="5m",
        eval_offset=60, probability_tickformer_v16=0.90,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_tickformer_writer_swallows_db_errors():
    """DB exceptions are logged but not propagated (fire-and-forget safety)."""
    db = DBClient.__new__(DBClient)

    class _BrokenConn:
        async def execute(self, *a, **kw):
            raise RuntimeError("simulated connection error")

    class _BrokenPool:
        def acquire(self, *, timeout=None):
            class _CM:
                async def __aenter__(self_inner):
                    return _BrokenConn()
                async def __aexit__(self_inner, *exc):
                    return None
            return _CM()

    db._pool = _BrokenPool()
    n = await db.update_signal_evaluations_tickformer(
        window_ts=1779000000, asset="BTC", timeframe="5m",
        eval_offset=60, probability_tickformer_v18=0.90,
    )
    assert n == 0  # Error swallowed, 0 rows affected.


# ── v20 column specifically ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tickformer_writer_v20_only():
    """v20 column is written when it's the only value populated."""
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_tickformer(
        window_ts=1779000000, asset="BTC", timeframe="5m",
        eval_offset=200, probability_tickformer_v20=0.93,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    args = call["args"]
    assert args[4] is None    # v16
    assert args[5] is None    # v17
    assert args[6] is None    # v18
    assert args[7] == 0.93    # v20
    assert "probability_tickformer_v20" in call["sql"]
