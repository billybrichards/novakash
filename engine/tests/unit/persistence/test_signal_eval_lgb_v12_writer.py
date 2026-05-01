"""Writer-regression tests for ``signal_evaluations.probability_lgb_v12``.

Audit-task #332 (writer regression #6). The column was added by
``migrations/add_probability_lgb_v12.sql`` but no engine code path ever
populated it. PR #438 wired ``window_snapshots`` only; the parallel
``signal_evaluations`` writer target stayed at 100% NULL.

Fix: ``DBClient.update_signal_evaluations_lgb_v12`` (mirrored on
``PgSignalRepository``) UPDATE-only stamper, fired from
``StrategyRegistry._write_window_trace`` immediately after the existing
``update_window_ensemble_fields`` call.

These tests pin the SQL contract so a future refactor cannot drop the
column from the writer again.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from persistence.db_client import DBClient


class _FakeConn:
    def __init__(self, result: str = "UPDATE 1") -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    async def execute(self, sql: str, *args, **kwargs):
        self.calls.append({"sql": sql, "args": args, "kwargs": kwargs})
        return self._result


class _FakePool:
    def __init__(self, result: str = "UPDATE 1") -> None:
        self.conn = _FakeConn(result)

    def acquire(self):
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _stub_db(result: str = "UPDATE 1") -> DBClient:
    db = DBClient.__new__(DBClient)
    db._pool = _FakePool(result)
    return db


# ─── DBClient.update_signal_evaluations_lgb_v12 ───────────────────────────

@pytest.mark.asyncio
async def test_v12_writer_stamps_when_eval_offset_present():
    db = _stub_db("UPDATE 3")
    n = await db.update_signal_evaluations_lgb_v12(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=60,
        probability_lgb_v12=0.6731,
    )
    assert n == 3
    assert len(db._pool.conn.calls) == 1
    call = db._pool.conn.calls[0]
    assert "UPDATE signal_evaluations" in call["sql"]
    assert "COALESCE(probability_lgb_v12" in call["sql"], "must preserve existing value"
    assert "eval_offset = $5" in call["sql"]
    assert call["args"][0] == 0.6731
    assert call["args"][1] == 1777617300
    assert call["args"][2] == "BTC"
    assert call["args"][3] == "5m"
    assert call["args"][4] == 60


@pytest.mark.asyncio
async def test_v12_writer_omits_eval_offset_clause_when_none():
    db = _stub_db("UPDATE 7")
    n = await db.update_signal_evaluations_lgb_v12(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=None,
        probability_lgb_v12=0.42,
    )
    assert n == 7
    sql = db._pool.conn.calls[0]["sql"]
    assert "eval_offset" not in sql, "no eval_offset arg → no eval_offset filter"
    args = db._pool.conn.calls[0]["args"]
    assert args == (0.42, 1777617300, "BTC", "5m")


@pytest.mark.asyncio
async def test_v12_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v12(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=0, probability_lgb_v12=None,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_v12_writer_noop_when_no_pool():
    db = DBClient.__new__(DBClient)
    db._pool = None
    n = await db.update_signal_evaluations_lgb_v12(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=0, probability_lgb_v12=0.5,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_v12_writer_swallows_db_errors():
    db = DBClient.__new__(DBClient)

    class _BoomConn:
        async def execute(self, *a, **k):
            raise RuntimeError("connection refused")

    class _BoomPool:
        def acquire(self):
            class _CM:
                async def __aenter__(self_inner):
                    return _BoomConn()

                async def __aexit__(self_inner, *exc):
                    return None

            return _CM()

    db._pool = _BoomPool()
    n = await db.update_signal_evaluations_lgb_v12(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=0, probability_lgb_v12=0.5,
    )
    assert n == 0  # logged + swallowed, never raises into orchestrator


@pytest.mark.asyncio
async def test_v12_writer_coerces_value_to_float():
    """asyncpg double-precision needs float, not Decimal/numpy."""
    db = _stub_db()
    await db.update_signal_evaluations_lgb_v12(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=0, probability_lgb_v12="0.55",  # type: ignore[arg-type]
    )
    args = db._pool.conn.calls[0]["args"]
    assert isinstance(args[0], float)
    assert args[0] == pytest.approx(0.55)


# ─── PgSignalRepository parity ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_pg_signal_repo_v12_writer_parity():
    """The Clean Architecture port must mirror DBClient byte-for-byte
    so the two writers can never drift again (lesson: PR #439 had to
    fix BOTH paths)."""
    from adapters.persistence.pg_signal_repo import PgSignalRepository

    pool = _FakePool("UPDATE 1")
    repo = PgSignalRepository(pool)  # type: ignore[arg-type]
    n = await repo.update_signal_evaluations_lgb_v12(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=60,
        probability_lgb_v12=0.6731,
    )
    assert n == 1
    sql = pool.conn.calls[0]["sql"]
    assert "UPDATE signal_evaluations" in sql
    assert "COALESCE(probability_lgb_v12" in sql
    assert "eval_offset = $5" in sql


# ─── Registry wiring ──────────────────────────────────────────────────────

class _SurfaceStub:
    """Minimal stand-in for FullDataSurface — only the fields the registry
    wiring path reads.  We bypass ``_ensemble_surface_fields`` and the
    trace_repo write via monkeypatch / AsyncMock so we do not need a
    real dataclass or trace adapter."""

    def __init__(self, *, eval_offset: int = 60, asset: str = "BTC") -> None:
        self.window_ts = 1777617300
        self.asset = asset
        self.timescale = "5m"
        self.eval_offset = eval_offset
        self.assembled_at = 0.0


def _stub_registry(db: AsyncMock) -> "Any":
    from strategies.registry import StrategyRegistry

    reg = StrategyRegistry.__new__(StrategyRegistry)
    reg._db = db
    reg._trace_repo = AsyncMock()
    reg._trace_repo.write_window_evaluation_trace = AsyncMock(return_value=None)
    reg._surface_trace_data = lambda surface: {}
    reg._log_async_write_error = lambda label: (lambda task: None)
    return reg


@pytest.mark.asyncio
async def test_registry_calls_v12_writer_after_ensemble_write(monkeypatch):
    """``_write_window_trace`` must invoke
    ``update_signal_evaluations_lgb_v12`` after the existing
    ``update_window_ensemble_fields`` call. Otherwise the column
    stays NULL forever (the original regression)."""
    import asyncio

    from strategies import five_min_vpin

    monkeypatch.setattr(
        five_min_vpin,
        "_ensemble_surface_fields",
        lambda surface: {
            "ensemble_p_up": 0.55,
            "ensemble_p_lgb": 0.6,
            "ensemble_p_classifier": None,
            "ensemble_mode": "lgb_only",
            "ensemble_disagreement": None,
            "ensemble_model_version": "v12-lgb-2026-04",
            "probability_lgb_v12": 0.71,
        },
    )
    # _v34_surface_fields is called earlier in _write_window_trace; stub it
    # to avoid touching the real surface dataclass internals.
    monkeypatch.setattr(
        five_min_vpin, "_v34_surface_fields", lambda surface: {}
    )

    db = AsyncMock()
    db.update_window_ensemble_fields = AsyncMock(return_value=None)
    db.update_signal_evaluations_lgb_v12 = AsyncMock(return_value=1)

    reg = _stub_registry(db)

    reg._write_window_trace(_SurfaceStub())  # type: ignore[arg-type]
    # Let the fire-and-forget tasks run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    db.update_signal_evaluations_lgb_v12.assert_awaited_once()
    kwargs = db.update_signal_evaluations_lgb_v12.await_args.kwargs
    assert kwargs["window_ts"] == 1777617300
    assert kwargs["asset"] == "BTC"
    assert kwargs["timeframe"] == "5m"
    assert kwargs["eval_offset"] == 60
    assert kwargs["probability_lgb_v12"] == 0.71


@pytest.mark.asyncio
async def test_registry_skips_v12_writer_when_value_missing(monkeypatch):
    """No probability_lgb_v12 in the surface → no parallel writer call.
    Avoids spamming the DB with no-op UPDATEs when timesfm is in fallback."""
    import asyncio

    from strategies import five_min_vpin

    monkeypatch.setattr(
        five_min_vpin,
        "_ensemble_surface_fields",
        lambda surface: {
            "ensemble_p_up": 0.5,
            "ensemble_p_lgb": None,
            "ensemble_p_classifier": None,
            "ensemble_mode": None,
            "ensemble_disagreement": None,
            "ensemble_model_version": None,
            "probability_lgb_v12": None,
        },
    )
    monkeypatch.setattr(
        five_min_vpin, "_v34_surface_fields", lambda surface: {}
    )

    db = AsyncMock()
    db.update_window_ensemble_fields = AsyncMock(return_value=None)
    db.update_signal_evaluations_lgb_v12 = AsyncMock(return_value=0)

    reg = _stub_registry(db)

    reg._write_window_trace(_SurfaceStub(eval_offset=0))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    db.update_signal_evaluations_lgb_v12.assert_not_awaited()
