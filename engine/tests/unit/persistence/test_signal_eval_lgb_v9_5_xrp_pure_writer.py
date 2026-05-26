"""Writer-regression tests for ``signal_evaluations.probability_lgb_v9_5_xrp_pure``.

This PR (feat/wire-v9_5_xrp_pure-consumer, 2026-05-26).

Mirrors test_signal_eval_lgb_v9_5_xrp_writer.py structure.

Covers:
- DBClient.update_signal_evaluations_lgb_v9_5_xrp_pure upserts correctly
- PgSignalRepository parity (SQL contract match)
- No-op when probability is None / eval_offset is None / no pool
- Swallows DB errors gracefully (fire-and-forget contract)
- Registry._write_window_trace calls the writer when PURE field is present
- Registry skips the writer when PURE field is None (forward compat)

Timesfm commit e1ba39d — V9_5_XRP_PURE_ENABLED=true.
Migration: signal_evaluations.probability_lgb_v9_5_xrp_pure (NUMERIC(10,6))
applied 2026-05-26. Sample value confirmed: 0.403.
RDS note #711 (overnight check).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

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


# ── DBClient.update_signal_evaluations_lgb_v9_5_xrp_pure ─────────────────

@pytest.mark.asyncio
async def test_xrp_pure_writer_upserts_row():
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_lgb_v9_5_xrp_pure(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_5_xrp_pure=0.403,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "probability_lgb_v9_5_xrp_pure" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "COALESCE" in sql
    args = call["args"]
    assert args[0] == 1779000000
    assert args[1] == "XRP"
    assert args[2] == "5m"
    assert args[3] == 120
    assert args[4] == pytest.approx(0.403)


@pytest.mark.asyncio
async def test_xrp_pure_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_5_xrp_pure(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_5_xrp_pure=None,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_xrp_pure_writer_noop_when_eval_offset_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_5_xrp_pure(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=None,
        probability_lgb_v9_5_xrp_pure=0.403,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_xrp_pure_writer_noop_when_no_pool():
    db = DBClient.__new__(DBClient)
    db._pool = None
    n = await db.update_signal_evaluations_lgb_v9_5_xrp_pure(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_5_xrp_pure=0.403,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_xrp_pure_writer_swallows_db_errors():
    class _BoomConn:
        async def execute(self, *a, **k):
            raise RuntimeError("boom")

    class _BoomPool:
        def acquire(self, **kwargs):
            class _CM:
                async def __aenter__(self_inner):
                    return _BoomConn()
                async def __aexit__(self_inner, *exc):
                    return None
            return _CM()

    db = DBClient.__new__(DBClient)
    db._pool = _BoomPool()
    n = await db.update_signal_evaluations_lgb_v9_5_xrp_pure(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_5_xrp_pure=0.403,
    )
    assert n == 0


# ── PgSignalRepository parity ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pg_signal_repo_xrp_pure_writer_parity():
    """PgSignalRepository mirrors DBClient SQL contract for XRP PURE."""
    from adapters.persistence.pg_signal_repo import PgSignalRepository

    pool = _FakePool("INSERT 0 1")
    repo = PgSignalRepository(pool)  # type: ignore[arg-type]
    n = await repo.update_signal_evaluations_lgb_v9_5_xrp_pure(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_5_xrp_pure=0.403,
    )
    assert n == 1
    sql = pool.conn.calls[0]["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "probability_lgb_v9_5_xrp_pure" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "COALESCE" in sql


# ── Registry writer fires when PURE field is present ─────────────────────

class _FullSurfaceStub:
    """Minimal FullDataSurface stand-in for registry wiring test."""

    def __init__(
        self,
        *,
        probability_lgb_v9_5_xrp_pure: float | None = None,
        probability_lgb_v9_5_xrp: float | None = None,
        asset: str = "XRP",
    ) -> None:
        self.window_ts = 1779000000
        self.asset = asset
        self.timescale = "5m"
        self.eval_offset = 120
        self.assembled_at = 0.0
        self.probability_lgb_v9_5_xrp_pure = probability_lgb_v9_5_xrp_pure
        self.probability_lgb_v9_5_xrp = probability_lgb_v9_5_xrp
        self.probability_lgb_v9_3_btc = None
        self.probability_lgb_v9_5_eth = None
        self.probability_lgb_v9_5_eth_pure = None
        self.probability_lgb_v9_2_eth = None
        self.probability_lgb_v9_2_post_iso = None
        self.probability_lgb_v9_2 = None
        self.probability_lgb_v9_1 = None
        self.probability_lgb_v12 = None
        self.probability_v2_meta_gate = None
        self.probability_v9_2_meta_gate = None
        self.probability_v12_meta_gate = None
        self.regime = None
        self.v2_probability_up = None
        self.probability_lgb = None
        self.probability_classifier = None
        self.ensemble_config = None


def _stub_registry(db: AsyncMock) -> Any:
    from strategies.registry import StrategyRegistry

    reg = StrategyRegistry.__new__(StrategyRegistry)
    reg._db = db
    reg._trace_repo = AsyncMock()
    reg._trace_repo.write_window_evaluation_trace = AsyncMock(return_value=None)
    reg._surface_trace_data = lambda surface: {}
    reg._log_async_write_error = lambda label: (lambda task: None)
    return reg


@pytest.mark.asyncio
async def test_registry_writes_xrp_pure_when_field_present(monkeypatch):
    """registry._write_window_trace fires update_signal_evaluations_lgb_v9_5_xrp_pure
    when probability_lgb_v9_5_xrp_pure is present on the surface."""
    import asyncio

    from strategies import five_min_vpin

    db = AsyncMock()
    db.update_signal_evaluations_lgb_v9_5_xrp_pure = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_5_xrp = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_3_btc = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_5_eth = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_5_eth_pure = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2_eth = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2_post_iso = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2 = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_1 = AsyncMock(return_value=1)
    db.update_window_ensemble_fields = AsyncMock(return_value=None)
    db.update_window_surface_fields = AsyncMock(return_value=None)
    monkeypatch.setattr(
        five_min_vpin, "_ensemble_surface_fields",
        lambda s: {
            "probability_lgb_v9_5_xrp_pure": s.probability_lgb_v9_5_xrp_pure,
            "probability_lgb_v9_5_xrp": None,
            "probability_lgb_v9_3_btc": None,
            "probability_lgb_v9_5_eth": None,
            "probability_lgb_v9_5_eth_pure": None,
            "probability_lgb_v9_2_eth": None,
            "probability_lgb_v9_2_post_iso": None,
            "probability_lgb_v9_2": None,
            "probability_lgb_v12": None,
            "probability_v2_meta_gate": None,
            "probability_v9_2_meta_gate": None,
            "probability_v12_meta_gate": None,
        },
    )

    surface = _FullSurfaceStub(
        probability_lgb_v9_5_xrp_pure=0.403,
        asset="XRP",
    )
    reg = _stub_registry(db)
    reg._write_window_trace(surface=surface)
    await asyncio.sleep(0)

    db.update_signal_evaluations_lgb_v9_5_xrp_pure.assert_awaited_once()
    call_kwargs = db.update_signal_evaluations_lgb_v9_5_xrp_pure.call_args
    assert call_kwargs.kwargs["probability_lgb_v9_5_xrp_pure"] == pytest.approx(0.403)
    assert call_kwargs.kwargs["window_ts"] == 1779000000
    assert call_kwargs.kwargs["asset"] == "XRP"
    assert call_kwargs.kwargs["eval_offset"] == 120


@pytest.mark.asyncio
async def test_registry_skips_xrp_pure_writer_when_field_none(monkeypatch):
    """registry._write_window_trace does NOT call the XRP PURE writer when
    probability_lgb_v9_5_xrp_pure is None on the surface."""
    import asyncio

    from strategies import five_min_vpin

    db = AsyncMock()
    db.update_signal_evaluations_lgb_v9_5_xrp_pure = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_5_xrp = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_3_btc = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_5_eth = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_5_eth_pure = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2_eth = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2_post_iso = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2 = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_1 = AsyncMock(return_value=1)
    db.update_window_ensemble_fields = AsyncMock(return_value=None)
    db.update_window_surface_fields = AsyncMock(return_value=None)
    monkeypatch.setattr(
        five_min_vpin, "_ensemble_surface_fields",
        lambda s: {
            "probability_lgb_v9_5_xrp_pure": None,
            "probability_lgb_v9_5_xrp": None,
            "probability_lgb_v9_3_btc": None,
            "probability_lgb_v9_5_eth": None,
            "probability_lgb_v9_5_eth_pure": None,
            "probability_lgb_v9_2_eth": None,
            "probability_lgb_v9_2_post_iso": None,
            "probability_lgb_v9_2": None,
            "probability_lgb_v12": None,
            "probability_v2_meta_gate": None,
            "probability_v9_2_meta_gate": None,
            "probability_v12_meta_gate": None,
        },
    )

    surface = _FullSurfaceStub(
        probability_lgb_v9_5_xrp_pure=None,
        asset="XRP",
    )
    reg = _stub_registry(db)
    reg._write_window_trace(surface=surface)
    await asyncio.sleep(0)

    db.update_signal_evaluations_lgb_v9_5_xrp_pure.assert_not_awaited()
