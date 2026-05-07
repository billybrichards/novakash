"""Writer-regression tests for ``signal_evaluations.probability_lgb_v9_2``.

PR #500 (this PR). Background:

- PR #499 added ``signal_evaluations.probability_lgb_v9_2`` + the four
  cohort metadata columns (v9_2_conviction, v9_2_pred_direction,
  v9_2_cohort, v9_2_gate_fired) and a sidecar UPDATE-only writer in
  ``StrategyRegistry._write_window_trace``.
- The sidecar writer ran every tick with eval_offset=current value,
  but the canonical INSERT (in ``write_signal_evaluation``) typically
  hadn't yet fired for that exact (window_ts, asset, timeframe,
  eval_offset). Result: the UPDATE matched 0 rows almost always —
  ``probability_lgb_v9_2`` stayed at 100% NULL the whole afternoon
  it was deployed.
- This PR converts the sidecar writers (v9_2 and v12) to
  INSERT...ON CONFLICT upserts. A minimal row is created when the
  canonical writer hasn't fired; a COALESCE-protected UPDATE merges
  the columns when it has.
- ``v9_2_gate_fired`` is OR-merged so that a TRADE-tick stamp wins
  over earlier SKIP-tick stamps within the same row.

Tests below pin the SQL contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

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

    def acquire(self):
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


# ─── DBClient.update_signal_evaluations_lgb_v9_2 ──────────────────────────

@pytest.mark.asyncio
async def test_v9_2_writer_upserts_row_with_full_cohort_metadata():
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=60,
        probability_lgb_v9_2=0.733,
        v9_2_conviction=0.733,
        v9_2_pred_direction="UP",
        v9_2_cohort="CASCADE_UP",
        v9_2_gate_fired=True,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "probability_lgb_v9_2" in sql
    assert "v9_2_conviction" in sql
    assert "v9_2_pred_direction" in sql
    assert "v9_2_cohort" in sql
    assert "v9_2_gate_fired" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    # gate_fired uses OR-merge, not COALESCE
    assert "OR COALESCE(EXCLUDED.v9_2_gate_fired" in sql
    args = call["args"]
    assert args[0] == 1777617300
    assert args[1] == "BTC"
    assert args[2] == "5m"
    assert args[3] == 60
    assert args[4] == pytest.approx(0.733)
    assert args[5] == pytest.approx(0.733)  # conviction
    assert args[6] == "UP"
    assert args[7] == "CASCADE_UP"
    assert args[8] is True


@pytest.mark.asyncio
async def test_v9_2_writer_upserts_with_partial_metadata():
    """Trace-time write seeds prob+conviction+direction+cohort but leaves
    gate_fired NULL. Must still upsert successfully."""
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1777617600,
        asset="BTC",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_2=0.4,
        v9_2_conviction=0.6,
        v9_2_pred_direction="DOWN",
        v9_2_cohort="NORMAL_DOWN",
        v9_2_gate_fired=None,
    )
    assert n == 1
    args = db._pool.conn.calls[0]["args"]
    assert args[8] is None  # gate_fired is NULL


@pytest.mark.asyncio
async def test_v9_2_writer_noop_when_eval_offset_none():
    """eval_offset is part of the unique key; writer must short-circuit."""
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=None, probability_lgb_v9_2=0.5,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_v9_2_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=60, probability_lgb_v9_2=None,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_v9_2_writer_noop_when_no_pool():
    db = DBClient.__new__(DBClient)
    db._pool = None
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=60, probability_lgb_v9_2=0.5,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_v9_2_writer_swallows_db_errors():
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
    n = await db.update_signal_evaluations_lgb_v9_2(
        window_ts=1, asset="BTC", timeframe="5m",
        eval_offset=60, probability_lgb_v9_2=0.5,
    )
    assert n == 0  # logged + swallowed


# ─── PgSignalRepository parity ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_pg_signal_repo_v9_2_writer_parity():
    """Clean Architecture port mirrors DBClient byte-for-byte."""
    from adapters.persistence.pg_signal_repo import PgSignalRepository

    pool = _FakePool("INSERT 0 1")
    repo = PgSignalRepository(pool)  # type: ignore[arg-type]
    n = await repo.update_signal_evaluations_lgb_v9_2(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=60,
        probability_lgb_v9_2=0.733,
        v9_2_conviction=0.733,
        v9_2_pred_direction="UP",
        v9_2_cohort="CASCADE_UP",
        v9_2_gate_fired=True,
    )
    assert n == 1
    sql = pool.conn.calls[0]["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "OR COALESCE(EXCLUDED.v9_2_gate_fired" in sql


# ─── Registry wiring ──────────────────────────────────────────────────────

class _SurfaceStub:
    """Minimal stand-in for FullDataSurface — only the fields the registry
    wiring path reads."""

    def __init__(
        self,
        *,
        eval_offset: int = 60,
        asset: str = "BTC",
        probability_lgb_v9_2: float | None = None,
        regime: str | None = None,
    ) -> None:
        self.window_ts = 1777617300
        self.asset = asset
        self.timescale = "5m"
        self.eval_offset = eval_offset
        self.assembled_at = 0.0
        self.probability_lgb_v9_2 = probability_lgb_v9_2
        self.regime = regime


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
async def test_registry_writes_v9_2_with_derived_cohort_metadata(monkeypatch):
    """At trace time the registry derives conviction / pred_direction /
    cohort from the surface and stamps them. gate_fired stays None until
    a strategy fires (handled separately by ``_stamp_v9_2_gate_fired``)."""
    import asyncio

    from strategies import five_min_vpin

    monkeypatch.setattr(
        five_min_vpin, "_ensemble_surface_fields", lambda surface: {}
    )
    monkeypatch.setattr(
        five_min_vpin, "_v34_surface_fields", lambda surface: {}
    )

    db = AsyncMock()
    db.update_signal_evaluations_lgb_v9_2 = AsyncMock(return_value=1)

    reg = _stub_registry(db)

    surface = _SurfaceStub(
        probability_lgb_v9_2=0.85,
        regime="CASCADE",
    )
    reg._write_window_trace(surface)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    db.update_signal_evaluations_lgb_v9_2.assert_awaited_once()
    kwargs = db.update_signal_evaluations_lgb_v9_2.await_args.kwargs
    assert kwargs["window_ts"] == 1777617300
    assert kwargs["asset"] == "BTC"
    assert kwargs["timeframe"] == "5m"
    assert kwargs["eval_offset"] == 60
    assert kwargs["probability_lgb_v9_2"] == pytest.approx(0.85)
    assert kwargs["v9_2_conviction"] == pytest.approx(0.85)
    assert kwargs["v9_2_pred_direction"] == "UP"
    assert kwargs["v9_2_cohort"] == "CASCADE_UP"
    assert kwargs["v9_2_gate_fired"] is None


@pytest.mark.asyncio
async def test_registry_writes_v9_2_with_down_direction_and_unknown_regime(
    monkeypatch,
):
    """p < 0.5 → DOWN. Missing regime falls back to UNKNOWN cohort key."""
    import asyncio

    from strategies import five_min_vpin

    monkeypatch.setattr(
        five_min_vpin, "_ensemble_surface_fields", lambda surface: {}
    )
    monkeypatch.setattr(
        five_min_vpin, "_v34_surface_fields", lambda surface: {}
    )

    db = AsyncMock()
    db.update_signal_evaluations_lgb_v9_2 = AsyncMock(return_value=1)

    reg = _stub_registry(db)

    surface = _SurfaceStub(
        eval_offset=120,
        probability_lgb_v9_2=0.3,
        regime=None,
    )
    reg._write_window_trace(surface)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    db.update_signal_evaluations_lgb_v9_2.assert_awaited_once()
    kwargs = db.update_signal_evaluations_lgb_v9_2.await_args.kwargs
    assert kwargs["probability_lgb_v9_2"] == pytest.approx(0.3)
    assert kwargs["v9_2_conviction"] == pytest.approx(0.7)  # max(0.3, 0.7)
    assert kwargs["v9_2_pred_direction"] == "DOWN"
    assert kwargs["v9_2_cohort"] == "UNKNOWN_DOWN"


@pytest.mark.asyncio
async def test_registry_skips_v9_2_writer_when_value_missing(monkeypatch):
    """No probability_lgb_v9_2 on the surface → no writer call. Avoids
    polluting the table with NULL stamps when the v9.2 booster is not
    loaded in timesfm-service."""
    import asyncio

    from strategies import five_min_vpin

    monkeypatch.setattr(
        five_min_vpin, "_ensemble_surface_fields", lambda surface: {}
    )
    monkeypatch.setattr(
        five_min_vpin, "_v34_surface_fields", lambda surface: {}
    )

    db = AsyncMock()
    db.update_signal_evaluations_lgb_v9_2 = AsyncMock(return_value=0)

    reg = _stub_registry(db)

    reg._write_window_trace(_SurfaceStub(probability_lgb_v9_2=None))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    db.update_signal_evaluations_lgb_v9_2.assert_not_awaited()


@pytest.mark.asyncio
async def test_stamp_v9_2_gate_fired_only_runs_on_trade():
    """``_stamp_v9_2_gate_fired`` must:
    - call writer with gate_fired=True when the v9_2 strategy emitted TRADE,
    - skip otherwise (SKIP / different strategy / surface missing v9_2).
    """
    import asyncio
    from types import SimpleNamespace

    db = AsyncMock()
    db.update_signal_evaluations_lgb_v9_2 = AsyncMock(return_value=1)
    reg = _stub_registry(db)

    surface = _SurfaceStub(probability_lgb_v9_2=0.92, regime="TRANSITION")

    # SKIP — no stamp.
    reg._stamp_v9_2_gate_fired(
        surface,
        [SimpleNamespace(strategy_id="v9_2_super_lgb_only", action="SKIP")],
    )
    await asyncio.sleep(0)
    db.update_signal_evaluations_lgb_v9_2.assert_not_awaited()

    # TRADE — stamp gate_fired=True with derived metadata.
    reg._stamp_v9_2_gate_fired(
        surface,
        [SimpleNamespace(strategy_id="v9_2_super_lgb_only", action="TRADE")],
    )
    await asyncio.sleep(0)
    db.update_signal_evaluations_lgb_v9_2.assert_awaited_once()
    kwargs = db.update_signal_evaluations_lgb_v9_2.await_args.kwargs
    assert kwargs["v9_2_gate_fired"] is True
    assert kwargs["v9_2_pred_direction"] == "UP"
    assert kwargs["v9_2_cohort"] == "TRANSITION_UP"

    # TRADE on a different strategy id — no stamp.
    db.update_signal_evaluations_lgb_v9_2.reset_mock()
    reg._stamp_v9_2_gate_fired(
        surface,
        [SimpleNamespace(strategy_id="v9_lgb_only", action="TRADE")],
    )
    await asyncio.sleep(0)
    db.update_signal_evaluations_lgb_v9_2.assert_not_awaited()


# ─── window_snapshots wiring (mirror v12 path for v9_2) ───────────────────

@pytest.mark.asyncio
async def test_update_window_ensemble_fields_includes_v9_2():
    """``update_window_ensemble_fields`` must upsert the new
    ``probability_lgb_v9_2`` column on window_snapshots so SQL analysis
    has first-class access to the v9.2 shadow probability (mirrors v12)."""
    db = _stub_db("INSERT 0 1")
    await db.update_window_ensemble_fields(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        eval_offset=60,
        ensemble_fields={
            "ensemble_p_up": 0.55,
            "ensemble_p_lgb": 0.6,
            "ensemble_p_classifier": None,
            "ensemble_mode": "lgb_only",
            "ensemble_disagreement": None,
            "ensemble_model_version": "v12",
            "probability_lgb_v12": 0.71,
            "probability_lgb_v9_2": 0.83,
        },
    )
    sql = db._pool.conn.calls[0]["sql"]
    assert "probability_lgb_v9_2" in sql
    assert "probability_lgb_v12" in sql
    args = db._pool.conn.calls[0]["args"]
    assert 0.83 in args
    assert 0.71 in args


@pytest.mark.asyncio
async def test_ensemble_surface_fields_includes_v9_2():
    """``_ensemble_surface_fields`` must extract probability_lgb_v9_2 from
    the surface so it flows into update_window_ensemble_fields."""
    from strategies.five_min_vpin import _ensemble_surface_fields
    from types import SimpleNamespace

    surface = SimpleNamespace(
        v2_probability_up=0.55,
        probability_lgb=0.6,
        probability_classifier=None,
        ensemble_config=None,
        probability_lgb_v12=0.71,
        probability_lgb_v9_2=0.83,
    )
    fields = _ensemble_surface_fields(surface)
    assert fields["probability_lgb_v9_2"] == 0.83
    assert fields["probability_lgb_v12"] == 0.71
