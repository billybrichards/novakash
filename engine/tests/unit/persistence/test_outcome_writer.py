"""Forward-writer regression tests (hub note 297, 2026-04-30).

Background
----------
For ~3 weeks ``window_snapshots.outcome`` and ``signal_evaluations.outcome``
were being silently dropped or polluted by the engine on resolution:

  * ``order_manager.poll_resolutions`` called
    ``DBClient.update_window_outcome`` with the trade's ``WIN`` / ``LOSS``
    label in the slot meant for the directional UP / DOWN / FLAT label.
    The column took the wrong values (729 polluted rows) and every
    downstream analysis script silently filtered them out as ``IS NULL``.

  * No engine path ever wrote ``signal_evaluations.outcome`` — 100% NULL
    post-Apr-8. ``v_signal_comparison`` view returned 0 rows.

Fix (PR: forward-outcome-writer):

  * ``update_window_outcome`` coerces ``outcome`` to UP/DOWN/FLAT using
    ``poly_winner`` as source of truth. WIN/LOSS without a directional
    poly_winner is silently ignored for the column.
  * ``COALESCE`` everywhere so re-deliveries cannot overwrite a
    correctly-resolved value.
  * New ``update_signal_evaluations_outcome`` bulk-fills the column for
    every row tied to a window.

These tests pin the new semantics — they will fail loudly if a future
refactor brings back the WIN/LOSS pollution or removes the
signal_evaluations writer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from persistence.db_client import DBClient


# ─── Coercion (pure) ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "outcome, poly_winner, expected",
    [
        # poly_winner is the source of truth — wins over outcome
        ("WIN", "Up", "UP"),
        ("LOSS", "Down", "DOWN"),
        ("WIN", "down", "DOWN"),
        # Direct UP/DOWN/FLAT also accepted
        ("UP", None, "UP"),
        ("down", None, "DOWN"),
        ("FLAT", None, "FLAT"),
        # WIN/LOSS without a poly_winner = no directional info → None
        ("WIN", None, None),
        ("LOSS", None, None),
        # Garbage in → None out
        ("", None, None),
        (None, None, None),
        ("RANDOM", "garbage", None),
        # Whitespace is tolerated
        (" up ", None, "UP"),
        (None, " Down\n", "DOWN"),
    ],
)
def test_coerce_directional_outcome(outcome, poly_winner, expected):
    assert DBClient._coerce_directional_outcome(outcome, poly_winner) == expected


# ─── update_window_outcome — SQL contract ─────────────────────────────────

class _FakeConn:
    """Records the SQL + params an asyncpg connection would have run."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, sql: str, *args, **kwargs):
        self.calls.append({"sql": sql, "args": args, "kwargs": kwargs})
        return "UPDATE 1"


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        # Return an async context manager that yields the conn.
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _stub_db_client() -> DBClient:
    """Build a DBClient with a fake pool — bypasses real connect()."""
    db = DBClient.__new__(DBClient)
    db._pool = _FakePool()
    return db


@pytest.mark.asyncio
async def test_update_window_outcome_writes_directional_from_poly_winner():
    """The 2026-04 regression: WIN/LOSS was being stored in outcome.

    With the fix, the directional label derived from poly_winner is
    written instead.
    """
    db = _stub_db_client()
    await db.update_window_outcome(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        outcome="WIN",        # legacy first arg — must NOT land in outcome col
        pnl_usd=12.34,
        poly_winner="Up",     # source of truth for direction
    )

    assert len(db._pool.conn.calls) == 1
    args = db._pool.conn.calls[0]["args"]
    # SQL is COALESCE(outcome, $1) — first positional is the directional label.
    assert args[0] == "UP", "must write UP, not WIN"
    assert args[1] == 12.34
    assert args[2] == "Up"
    assert args[3] == 1777617300
    assert args[4] == "BTC"
    assert args[5] == "5m"


@pytest.mark.asyncio
async def test_update_window_outcome_drops_non_directional_inputs():
    """If neither outcome nor poly_winner are directional, the
    coerced value is NULL — never WIN/LOSS — so COALESCE leaves the
    column untouched.
    """
    db = _stub_db_client()
    await db.update_window_outcome(
        window_ts=1, asset="BTC", timeframe="5m",
        outcome="WIN", pnl_usd=0.0, poly_winner=None,
    )
    args = db._pool.conn.calls[0]["args"]
    assert args[0] is None, "WIN without poly_winner must coerce to None"


@pytest.mark.asyncio
async def test_update_window_outcome_coalesces_every_column():
    """Idempotency contract: every UPDATEd column wraps in COALESCE so
    re-deliveries cannot overwrite a real resolution with stale data.
    """
    db = _stub_db_client()
    await db.update_window_outcome(
        window_ts=1, asset="BTC", timeframe="5m",
        outcome="UP", pnl_usd=1.0, poly_winner="Up",
    )
    sql = db._pool.conn.calls[0]["sql"]
    assert "COALESCE(outcome" in sql
    assert "COALESCE(pnl_usd" in sql
    assert "COALESCE(poly_winner" in sql


@pytest.mark.asyncio
async def test_update_window_outcome_no_pool_is_noop():
    db = DBClient.__new__(DBClient)
    db._pool = None
    # Must not raise.
    await db.update_window_outcome(
        window_ts=1, asset="BTC", timeframe="5m",
        outcome="UP", pnl_usd=1.0, poly_winner="Up",
    )


# ─── update_signal_evaluations_outcome ────────────────────────────────────

@pytest.mark.asyncio
async def test_signal_evaluations_outcome_writes_when_directional():
    db = _stub_db_client()
    n = await db.update_signal_evaluations_outcome(
        window_ts=1777617300, asset="BTC", timeframe="5m", outcome="UP",
    )
    assert n == 1  # FakeConn.execute returned "UPDATE 1"
    assert len(db._pool.conn.calls) == 1
    args = db._pool.conn.calls[0]["args"]
    assert args[0] == "UP"
    assert args[1] == 1777617300
    assert args[2] == "BTC"
    assert args[3] == "5m"
    sql = db._pool.conn.calls[0]["sql"]
    assert "outcome IS NULL" in sql, "must be idempotent — fill NULL only"


@pytest.mark.asyncio
async def test_signal_evaluations_outcome_skips_non_directional():
    """``WIN`` / ``LOSS`` / garbage must NOT touch the table."""
    db = _stub_db_client()
    n = await db.update_signal_evaluations_outcome(
        window_ts=1, asset="BTC", timeframe="5m", outcome="WIN",
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_signal_evaluations_outcome_handles_lowercase():
    db = _stub_db_client()
    n = await db.update_signal_evaluations_outcome(
        window_ts=1, asset="BTC", timeframe="5m", outcome="down",
    )
    assert n == 1
    args = db._pool.conn.calls[0]["args"]
    assert args[0] == "DOWN"


# ─── update_shadow_resolution — outcome forward write ─────────────────────

@pytest.mark.asyncio
async def test_shadow_resolution_writes_outcome():
    """The shadow resolution loop must populate the canonical
    outcome column, not just oracle_outcome.
    """
    db = _stub_db_client()
    await db.update_shadow_resolution(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        oracle_outcome="UP",
        shadow_pnl=1.50,
        shadow_would_win=True,
    )
    assert len(db._pool.conn.calls) == 1
    sql = db._pool.conn.calls[0]["sql"]
    args = db._pool.conn.calls[0]["args"]
    assert "outcome" in sql, "must write the canonical outcome column"
    assert "COALESCE(outcome" in sql
    # Param order in SQL: $1 oracle_outcome, $2 shadow_pnl, $3 shadow_would_win,
    #                    $4 directional outcome, $5 ts, $6 asset, $7 tf
    assert args[0] == "UP"            # oracle_outcome
    assert args[1] == 1.50            # shadow_pnl
    assert args[2] is True            # shadow_would_win
    assert args[3] == "UP"            # coerced directional outcome
    assert args[4] == 1777617300
    assert args[5] == "BTC"
    assert args[6] == "5m"
