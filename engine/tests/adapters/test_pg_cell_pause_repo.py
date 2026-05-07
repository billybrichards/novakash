"""Unit tests for PgCellPauseRepo.

Uses an in-memory asyncpg-compatible mock — no live DB required.
Tests: INSERT, list active (with and without rows), release, is_cell_paused,
get_recent_trades_for_cell (empty case), graceful degradation when pool=None.

Audits #379 + #385 (2026-05-06).
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.persistence.pg_cell_pause_repo import PgCellPauseRepo
from services.rolling_wr_monitor import CellKey


# ── lightweight asyncpg stub ─────────────────────────────────────────────────

class FakeConn:
    """Minimal asyncpg.Connection stub backed by an in-memory store."""

    def __init__(self, store: "FakeStore"):
        self._store = store

    async def execute(self, query: str, *args) -> None:
        # DDL execute — no-op for our tests
        pass

    async def fetchrow(self, query: str, *args) -> Optional[Dict]:
        return self._store.fetchrow(query, *args)

    async def fetch(self, query: str, *args) -> List[Dict]:
        return self._store.fetch(query, *args)


class FakeStore:
    """Stores rows in memory, implements minimal query routing."""

    def __init__(self):
        self._pauses: Dict[int, Dict] = {}
        self._trades: List[Dict] = {}
        self._next_id = 1

    def fetchrow(self, query: str, *args) -> Optional[Dict]:
        q = query.strip().lower()
        if "insert into cell_pauses" in q:
            # Check for conflict (active pause for same cell)
            strat_id, direction, t_band, regime, session_, pause_secs, reason, metric = args
            for row in self._pauses.values():
                if (
                    row["strategy_id"] == strat_id
                    and row["direction"] == direction
                    and row["t_band"] == t_band
                    and row["regime"] == regime
                    and row["session"] == session_
                    and row["released_at"] is None
                ):
                    return None  # conflict — ON CONFLICT DO NOTHING
            now = datetime.now(timezone.utc)
            pause_id = self._next_id
            self._next_id += 1
            self._pauses[pause_id] = {
                "id": pause_id,
                "strategy_id": strat_id,
                "direction": direction,
                "t_band": t_band,
                "regime": regime,
                "session": session_,
                "paused_at": now,
                "pause_until": now + timedelta(seconds=int(pause_secs)),
                "reason": reason,
                "trigger_metric": json.loads(metric) if metric else {},
                "released_at": None,
                "released_by": None,
            }
            return {"id": pause_id}
        if "select 1" in q and "cell_pauses" in q:
            strat_id, direction, t_band, regime, session_ = args
            now = datetime.now(timezone.utc)
            for row in self._pauses.values():
                if (
                    row["strategy_id"] == strat_id
                    and row["direction"] == direction
                    and row["t_band"] == t_band
                    and row["regime"] == regime
                    and row["session"] == session_
                    and row["released_at"] is None
                    and row["pause_until"] > now
                ):
                    return {"1": 1}
            return None
        if "count(*)" in q and "trades" in q:
            return {"wins": 0, "total": 0}
        return None

    def fetch(self, query: str, *args) -> List[Dict]:
        q = query.strip().lower()
        if "from cell_pauses" in q and "released_at is null" in q:
            now = datetime.now(timezone.utc)
            result = []
            for row in self._pauses.values():
                if row["released_at"] is None and row["pause_until"] > now:
                    result.append(dict(row))
            return result
        if "from trades" in q:
            return []
        return []

    async def execute_update(self, query: str, *args) -> None:
        q = query.strip().lower()
        if "update cell_pauses" in q and "released_at" in q:
            pause_id, released_by = int(args[0]), args[1]
            if pause_id in self._pauses:
                self._pauses[pause_id]["released_at"] = datetime.now(timezone.utc)
                self._pauses[pause_id]["released_by"] = released_by


class FakePool:
    def __init__(self):
        self._store = FakeStore()

    @asynccontextmanager
    async def acquire(self):
        conn = FakeConn(self._store)
        # Patch execute for UPDATE operations
        original_execute = conn.execute

        async def patched_execute(query: str, *args):
            q = query.strip().lower()
            if "update cell_pauses" in q and "released_at" in q:
                await conn._store.execute_update(query, *args)
            else:
                await original_execute(query, *args)

        conn.execute = patched_execute
        yield conn


# ── helper ───────────────────────────────────────────────────────────────────

def _make_cell(
    strategy_id: str = "test_strategy",
    direction: str = "DOWN",
    t_band: str = "T-61-90",
    regime: Optional[str] = "CASCADE",
    session: Optional[str] = "eu_am",
) -> CellKey:
    return CellKey(
        strategy_id=strategy_id,
        direction=direction,
        t_band=t_band,
        regime=regime,
        session=session,
    )


# ── tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_insert_pause_returns_id():
    pool = FakePool()
    repo = PgCellPauseRepo(pool=pool)
    cell = _make_cell()

    pause_id = await repo.insert_pause(
        cell,
        pause_seconds=3600,
        reason="test reason",
        trigger_metric={"wr": 0.3, "n": 5},
    )

    assert pause_id is not None
    assert isinstance(pause_id, int)
    assert pause_id >= 1


@pytest.mark.asyncio
async def test_insert_pause_collision_returns_none():
    """Second insert for same active cell returns None (conflict)."""
    pool = FakePool()
    repo = PgCellPauseRepo(pool=pool)
    cell = _make_cell()

    id1 = await repo.insert_pause(
        cell, 3600, "first", {"wr": 0.3, "n": 5}
    )
    id2 = await repo.insert_pause(
        cell, 3600, "second", {"wr": 0.25, "n": 6}
    )

    assert id1 is not None
    assert id2 is None  # collision


@pytest.mark.asyncio
async def test_is_cell_paused_true_after_insert():
    pool = FakePool()
    repo = PgCellPauseRepo(pool=pool)
    cell = _make_cell()

    assert await repo.is_cell_paused(cell) is False

    await repo.insert_pause(cell, 3600, "reason", {"n": 5})

    assert await repo.is_cell_paused(cell) is True


@pytest.mark.asyncio
async def test_release_pause_clears_active():
    pool = FakePool()
    repo = PgCellPauseRepo(pool=pool)
    cell = _make_cell()

    pause_id = await repo.insert_pause(cell, 3600, "r", {"n": 5})
    assert await repo.is_cell_paused(cell) is True

    await repo.release_pause(pause_id, released_by="ops")
    # After release, the row's released_at is set — so is_cell_paused checks
    # the DB which now sees released_at IS NOT NULL. Fake store honours this.
    # The FakeConn's is_cell_paused SELECT checks released_at IS NULL.
    paused = pool._store._pauses[pause_id]
    assert paused["released_at"] is not None


@pytest.mark.asyncio
async def test_list_active_pauses_empty_initially():
    pool = FakePool()
    repo = PgCellPauseRepo(pool=pool)

    rows = await repo.list_active_pauses()
    assert rows == []


@pytest.mark.asyncio
async def test_list_active_pauses_returns_inserted():
    pool = FakePool()
    repo = PgCellPauseRepo(pool=pool)
    cell = _make_cell()

    await repo.insert_pause(cell, 3600, "test", {"n": 5})
    rows = await repo.list_active_pauses()

    assert len(rows) == 1
    assert rows[0]["strategy_id"] == cell.strategy_id
    assert rows[0]["direction"] == cell.direction


@pytest.mark.asyncio
async def test_no_pool_returns_safe_defaults():
    """All methods return safe falsy values when pool is None."""
    repo = PgCellPauseRepo()  # no pool, no db_client
    cell = _make_cell()

    assert await repo.is_cell_paused(cell) is False
    assert await repo.insert_pause(cell, 3600, "r", {}) is None
    assert await repo.get_recent_trades_for_cell(cell, 3600) == []
    assert await repo.get_baseline_wr(cell, 7 * 86400) is None
    assert await repo.list_active_pauses() == []
    # release must not raise
    await repo.release_pause(999, "ops")


@pytest.mark.asyncio
async def test_get_recent_trades_empty_when_no_trades():
    pool = FakePool()
    repo = PgCellPauseRepo(pool=pool)
    cell = _make_cell()

    trades = await repo.get_recent_trades_for_cell(cell, lookback_seconds=3600)
    assert trades == []


@pytest.mark.asyncio
async def test_get_baseline_wr_none_when_insufficient_data():
    pool = FakePool()
    repo = PgCellPauseRepo(pool=pool)
    cell = _make_cell()

    # FakeStore returns {wins: 0, total: 0} — below 10 threshold
    result = await repo.get_baseline_wr(cell, lookback_seconds=7 * 86400)
    assert result is None


# ── B1 fix: session bucketed by entry time (created_at), not resolution time ──


@pytest.mark.asyncio
async def test_get_recent_trades_sql_uses_created_at():
    """B1 fix: get_recent_trades_for_cell must SELECT EXTRACT(HOUR FROM created_at)
    not resolved_at. Verified by inspecting the query string sent to the fake conn.
    """
    import re

    captured_query = []

    class CapturingConn:
        async def fetch(self, query: str, *args):
            captured_query.append(query)
            return []

    class CapturingPool:
        @asynccontextmanager
        async def acquire(self):
            yield CapturingConn()

    repo = PgCellPauseRepo(pool=CapturingPool())
    cell = _make_cell()
    await repo.get_recent_trades_for_cell(cell, lookback_seconds=3600)

    assert captured_query, "no query was issued"
    q = captured_query[0].lower()
    # Must use created_at for hour extraction, not resolved_at
    assert "extract(hour from created_at)" in q, (
        "B1: get_recent_trades_for_cell must bucket session from created_at, "
        f"not resolved_at. Query: {captured_query[0][:300]}"
    )
    assert "extract(hour from resolved_at)" not in q, (
        "B1: resolved_at should NOT be used for session bucketing"
    )


# ── B3 fix: get_baseline_wr_python filters to same cell axes ─────────────────


@pytest.mark.asyncio
async def test_baseline_wr_python_filters_by_cell_axes():
    """B3 fix: _get_baseline_wr_python must filter to the same t_band, session,
    regime as the cell — not aggregate all trades for the strategy.

    Trade fired at hour 16 UTC (us_pm) with eval_offset=88 (T-61-90) and
    regime CASCADE should match cell (us_pm, T-61-90, CASCADE) but NOT
    a cell (us_pm, T-61-90, NORMAL) or (eu_am, T-61-90, CASCADE).
    """
    from datetime import datetime, timezone
    from contextlib import asynccontextmanager

    # Build a set of fake rows: mix of matching and non-matching cells.
    fake_rows = [
        # Matching: us_pm, T-61-90, CASCADE — 7 wins + 3 losses
        *[{"outcome": "WIN", "eval_offset": 88, "hour_utc": 16, "regime": "CASCADE"}] * 7,
        *[{"outcome": "LOSS", "eval_offset": 88, "hour_utc": 16, "regime": "CASCADE"}] * 3,
        # Non-matching regime: should be excluded
        *[{"outcome": "LOSS", "eval_offset": 88, "hour_utc": 16, "regime": "NORMAL"}] * 20,
        # Non-matching t_band (eval_offset=50 => T-31-60): excluded
        *[{"outcome": "LOSS", "eval_offset": 50, "hour_utc": 16, "regime": "CASCADE"}] * 10,
        # Non-matching session (hour 10 = eu_am): excluded
        *[{"outcome": "LOSS", "eval_offset": 88, "hour_utc": 10, "regime": "CASCADE"}] * 10,
    ]

    class FakeBaselineConn:
        async def fetch(self, query: str, *args):
            return fake_rows

    class FakeBaselinePool:
        @asynccontextmanager
        async def acquire(self):
            yield FakeBaselineConn()

    repo = PgCellPauseRepo(pool=FakeBaselinePool())
    cell = CellKey(
        strategy_id="v12_lgb_combo",
        direction="DOWN",
        t_band="T-61-90",
        regime="CASCADE",
        session="us_pm",
    )

    result = await repo._get_baseline_wr_python(cell, lookback_seconds=7 * 86400)
    assert result is not None, "Should have enough matching trades (10)"
    assert abs(result - 0.70) < 0.01, (
        f"B3: baseline should be 7/10=0.70 (only matching cell trades), got {result}"
    )


@pytest.mark.asyncio
async def test_baseline_wr_python_none_below_threshold():
    """B3: returns None when fewer than 10 trades match the cell."""
    from contextlib import asynccontextmanager

    # Only 5 matching trades
    fake_rows = [
        *[{"outcome": "WIN", "eval_offset": 88, "hour_utc": 16, "regime": "CASCADE"}] * 3,
        *[{"outcome": "LOSS", "eval_offset": 88, "hour_utc": 16, "regime": "CASCADE"}] * 2,
    ]

    class FakeConn2:
        async def fetch(self, query: str, *args):
            return fake_rows

    class FakePool2:
        @asynccontextmanager
        async def acquire(self):
            yield FakeConn2()

    repo = PgCellPauseRepo(pool=FakePool2())
    cell = CellKey(
        strategy_id="v12_lgb_combo",
        direction="DOWN",
        t_band="T-61-90",
        regime="CASCADE",
        session="us_pm",
    )
    result = await repo._get_baseline_wr_python(cell, lookback_seconds=7 * 86400)
    assert result is None
