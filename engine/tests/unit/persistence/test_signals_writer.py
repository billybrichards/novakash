"""Regression tests for the ``signals`` writer schema-drift fix.

Pre-fix the writer issued ``INSERT INTO signals (signal_type, value,
metadata, created_at)`` but the table schema is
``signals(id, signal_type, payload JSONB, created_at)``. Every VPIN
write produced ``column "value" of relation "signals" does not exist``.

Hub readers (``hub/api/paper.py``, ``hub/api/v58_monitor.py``) already
read the new shape — they unpack ``payload->>'value'`` and
``payload->>'btc_price'`` — so the writer was the only stale piece.

These tests pin the new contract: payload merges value + metadata into
a single JSONB column.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from persistence.db_client import DBClient


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, sql: str, *args, **kwargs):
        self.calls.append({"sql": sql, "args": args, "kwargs": kwargs})
        return "INSERT 0 1"


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _stub_db() -> DBClient:
    db = DBClient.__new__(DBClient)
    db._pool = _FakePool()
    return db


@pytest.mark.asyncio
async def test_write_signal_uses_payload_jsonb_column():
    db = _stub_db()
    await db.write_signal("vpin", 0.687)

    assert len(db._pool.conn.calls) == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "INSERT INTO signals (signal_type, payload, created_at)" in sql
    assert "value" not in sql.split("INSERT INTO signals")[1].split("VALUES")[0]
    assert "metadata" not in sql.split("INSERT INTO signals")[1].split("VALUES")[0]


@pytest.mark.asyncio
async def test_write_signal_folds_value_into_payload():
    db = _stub_db()
    await db.write_signal("vpin", 0.687)

    args = db._pool.conn.calls[0]["args"]
    assert args[0] == "vpin"
    payload = json.loads(args[1])
    assert payload == {"value": 0.687}


@pytest.mark.asyncio
async def test_write_signal_merges_metadata_with_value():
    db = _stub_db()
    await db.write_signal(
        "tick",
        78400.5,
        metadata={"btc_price": 78400.5, "ts": 1777617300},
    )
    args = db._pool.conn.calls[0]["args"]
    payload = json.loads(args[1])
    assert payload == {"value": 78400.5, "btc_price": 78400.5, "ts": 1777617300}


@pytest.mark.asyncio
async def test_write_signal_value_coerces_to_float():
    db = _stub_db()
    await db.write_signal("vpin", "0.55")  # type: ignore[arg-type]
    payload = json.loads(db._pool.conn.calls[0]["args"][1])
    assert isinstance(payload["value"], float)
    assert payload["value"] == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_write_signal_no_metadata_yields_value_only_payload():
    db = _stub_db()
    await db.write_signal("vpin", 0.42, metadata=None)
    payload = json.loads(db._pool.conn.calls[0]["args"][1])
    assert payload == {"value": 0.42}


@pytest.mark.asyncio
async def test_pg_signal_repo_write_signal_parity():
    """The Clean Architecture port must mirror DBClient byte-for-byte
    so the two writers cannot drift again (PR #439 lesson)."""
    from adapters.persistence.pg_signal_repo import PgSignalRepository

    pool = _FakePool()
    repo = PgSignalRepository(pool)  # type: ignore[arg-type]
    await repo.write_signal("vpin", 0.687, metadata={"asset": "BTC"})

    sql = pool.conn.calls[0]["sql"]
    assert "INSERT INTO signals (signal_type, payload, created_at)" in sql
    payload = json.loads(pool.conn.calls[0]["args"][1])
    assert payload == {"value": 0.687, "asset": "BTC"}
