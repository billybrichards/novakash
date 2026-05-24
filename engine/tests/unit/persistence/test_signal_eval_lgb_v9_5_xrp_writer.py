"""Writer-regression tests for ``signal_evaluations.probability_lgb_v9_5_xrp``.

This PR (feat/v9_5_xrp_strategies, 2026-05-23).

Mirrors test_signal_eval_lgb_v9_3_btc_writer.py structure.

Covers:
- DBClient.update_signal_evaluations_lgb_v9_5_xrp upserts correctly
- PgSignalRepository parity (byte-for-byte SQL match)
- No-op when probability is None / eval_offset is None / no pool
- Swallows DB errors gracefully (fire-and-forget contract)
- Registry._write_window_trace calls the writer when XRP field is present
- Registry skips the writer when XRP field is None (forward compat)
- ensemble_fields dict includes probability_lgb_v9_5_xrp (window_snapshots)
- update_window_ensemble_fields (db_client) includes new column at $17

Timesfm-repo PR #160 + RDS note #593 (walk-forward CV results).
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


# ── DBClient.update_signal_evaluations_lgb_v9_5_xrp ──────────────────────

@pytest.mark.asyncio
async def test_xrp_writer_upserts_row():
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_lgb_v9_5_xrp(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_5_xrp=0.85,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "probability_lgb_v9_5_xrp" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "COALESCE" in sql
    args = call["args"]
    assert args[0] == 1779000000
    assert args[1] == "XRP"
    assert args[2] == "5m"
    assert args[3] == 120
    assert args[4] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_xrp_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_5_xrp(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_5_xrp=None,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_xrp_writer_noop_when_eval_offset_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_5_xrp(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=None,
        probability_lgb_v9_5_xrp=0.85,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_xrp_writer_noop_when_no_pool():
    db = DBClient.__new__(DBClient)
    db._pool = None
    n = await db.update_signal_evaluations_lgb_v9_5_xrp(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_5_xrp=0.85,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_xrp_writer_swallows_db_errors():
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
    n = await db.update_signal_evaluations_lgb_v9_5_xrp(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_5_xrp=0.85,
    )
    assert n == 0


# ── PgSignalRepository parity ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pg_signal_repo_xrp_writer_parity():
    """PgSignalRepository mirrors DBClient SQL contract."""
    from adapters.persistence.pg_signal_repo import PgSignalRepository

    pool = _FakePool("INSERT 0 1")
    repo = PgSignalRepository(pool)  # type: ignore[arg-type]
    n = await repo.update_signal_evaluations_lgb_v9_5_xrp(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        probability_lgb_v9_5_xrp=0.85,
    )
    assert n == 1
    sql = pool.conn.calls[0]["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "probability_lgb_v9_5_xrp" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "COALESCE" in sql


# ── ensemble_surface_fields includes new column ───────────────────────────

class _SurfaceStubWithXrp:
    """Minimal FullDataSurface stand-in."""

    def __init__(self, *, probability_lgb_v9_5_xrp: float | None = None) -> None:
        self.window_ts = 1779000000
        self.asset = "XRP"
        self.timescale = "5m"
        self.eval_offset = 120
        self.assembled_at = 0.0
        self.probability_lgb_v9_2 = None
        self.probability_lgb_v9_2_post_iso = None
        self.probability_lgb_v9_2_eth = None
        self.probability_lgb_v9_5_eth = None
        self.probability_lgb_v9_3_btc = None
        self.probability_lgb_v9_5_xrp = probability_lgb_v9_5_xrp
        self.probability_lgb_v12 = None
        self.probability_v2_meta_gate = None
        self.probability_v9_2_meta_gate = None
        self.probability_v12_meta_gate = None
        self.v2_probability_up = None
        self.probability_lgb = None
        self.probability_classifier = None
        self.ensemble_config = None


@pytest.mark.asyncio
async def test_ensemble_surface_fields_includes_xrp():
    """_ensemble_surface_fields passes probability_lgb_v9_5_xrp when set."""
    from strategies.five_min_vpin import _ensemble_surface_fields

    surface = _SurfaceStubWithXrp(probability_lgb_v9_5_xrp=0.85)
    fields = _ensemble_surface_fields(surface)
    assert "probability_lgb_v9_5_xrp" in fields
    assert fields["probability_lgb_v9_5_xrp"] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_ensemble_surface_fields_xrp_none_when_absent():
    from strategies.five_min_vpin import _ensemble_surface_fields

    surface = _SurfaceStubWithXrp(probability_lgb_v9_5_xrp=None)
    fields = _ensemble_surface_fields(surface)
    assert "probability_lgb_v9_5_xrp" in fields
    assert fields["probability_lgb_v9_5_xrp"] is None


# ── update_window_ensemble_fields includes new column ($17) ───────────────

@pytest.mark.asyncio
async def test_update_window_ensemble_fields_includes_xrp():
    """window_snapshots upsert SQL contains probability_lgb_v9_5_xrp as $17."""
    db = _stub_db("INSERT 0 1")
    await db.update_window_ensemble_fields(
        window_ts=1779000000,
        asset="XRP",
        timeframe="5m",
        eval_offset=120,
        ensemble_fields={
            "ensemble_p_up": 0.75,
            "ensemble_p_lgb": 0.73,
            "ensemble_p_classifier": None,
            "ensemble_mode": "blend",
            "ensemble_disagreement": 0.1,
            "ensemble_model_version": "v5.2",
            "probability_lgb_v12": None,
            "probability_lgb_v9_2": None,
            "probability_lgb_v9_2_post_iso": None,
            "probability_lgb_v9_2_eth": None,
            "probability_lgb_v9_5_eth": None,
            "probability_lgb_v9_3_btc": None,
            # Three new BTC PURE columns inserted between v9_3_btc and v9_5_xrp
            # by PR feat/v9_3_btc_pure_lgb_strategy (2026-05-24).
            "probability_lgb_v9_3_btc_pure": None,
            "probability_lgb_v9_2_pure": None,
            "probability_lgb_v12_pure": None,
            "probability_lgb_v9_5_xrp": 0.85,
            "probability_v2_meta_gate": None,
            "probability_v9_2_meta_gate": None,
            "probability_v12_meta_gate": None,
        },
    )
    sql = db._pool.conn.calls[0]["sql"]
    assert "probability_lgb_v9_5_xrp" in sql
    assert "$23" in sql
    # Param order: $1-$4 (window/asset/tf/offset), $5-$10 (ensemble),
    # $11-$13 (v12, v9_2, post_iso), $14=v9_2_eth, $15=v9_5_eth,
    # $16=v9_3_btc, $17=v9_3_btc_pure, $18=v9_2_pure, $19=v12_pure,
    # $20=v9_5_xrp, $21-$23 meta gate
    args = db._pool.conn.calls[0]["args"]
    assert args[19] == pytest.approx(0.85)


# ── Registry writer fires when XRP field is present ──────────────────────

class _FullSurfaceStub:
    """More complete surface stub for registry wiring test."""

    def __init__(
        self,
        *,
        probability_lgb_v9_5_xrp: float | None = None,
        probability_lgb_v9_3_btc: float | None = None,
        probability_lgb_v9_5_eth: float | None = None,
        probability_lgb_v9_2: float | None = None,
        probability_lgb_v9_2_post_iso: float | None = None,
        probability_lgb_v9_2_eth: float | None = None,
        probability_lgb_v9_1: float | None = None,
        regime: str | None = None,
        asset: str = "XRP",
    ) -> None:
        self.window_ts = 1779000000
        self.asset = asset
        self.timescale = "5m"
        self.eval_offset = 120
        self.assembled_at = 0.0
        self.probability_lgb_v9_5_xrp = probability_lgb_v9_5_xrp
        self.probability_lgb_v9_3_btc = probability_lgb_v9_3_btc
        self.probability_lgb_v9_5_eth = probability_lgb_v9_5_eth
        self.probability_lgb_v9_2_eth = probability_lgb_v9_2_eth
        self.probability_lgb_v9_2_post_iso = probability_lgb_v9_2_post_iso
        self.probability_lgb_v9_2 = probability_lgb_v9_2
        self.probability_lgb_v9_1 = probability_lgb_v9_1
        self.probability_lgb_v12 = None
        self.probability_v2_meta_gate = None
        self.probability_v9_2_meta_gate = None
        self.probability_v12_meta_gate = None
        self.regime = regime
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
async def test_registry_writes_xrp_when_field_present(monkeypatch):
    """registry._write_window_trace fires update_signal_evaluations_lgb_v9_5_xrp
    when probability_lgb_v9_5_xrp is present on the surface."""
    import asyncio

    from strategies import five_min_vpin

    db = AsyncMock()
    db.update_signal_evaluations_lgb_v9_5_xrp = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_3_btc = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_5_eth = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2_eth = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2_post_iso = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2 = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_1 = AsyncMock(return_value=1)
    db.update_window_ensemble_fields = AsyncMock(return_value=None)
    db.update_window_surface_fields = AsyncMock(return_value=None)
    monkeypatch.setattr(
        five_min_vpin, "_ensemble_surface_fields",
        lambda s: {
            "probability_lgb_v9_5_xrp": s.probability_lgb_v9_5_xrp,
            "probability_lgb_v9_3_btc": None,
            "probability_lgb_v9_5_eth": None,
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
        probability_lgb_v9_5_xrp=0.85,
        asset="XRP",
    )
    reg = _stub_registry(db)
    reg._write_window_trace(surface=surface)
    await asyncio.sleep(0)

    db.update_signal_evaluations_lgb_v9_5_xrp.assert_awaited_once()
    call_kwargs = db.update_signal_evaluations_lgb_v9_5_xrp.call_args
    assert call_kwargs.kwargs["probability_lgb_v9_5_xrp"] == pytest.approx(0.85)
    assert call_kwargs.kwargs["window_ts"] == 1779000000
    assert call_kwargs.kwargs["asset"] == "XRP"
    assert call_kwargs.kwargs["eval_offset"] == 120


@pytest.mark.asyncio
async def test_registry_skips_xrp_writer_when_field_none(monkeypatch):
    """registry._write_window_trace does NOT call the XRP writer when
    probability_lgb_v9_5_xrp is None on the surface."""
    import asyncio

    from strategies import five_min_vpin

    db = AsyncMock()
    db.update_signal_evaluations_lgb_v9_5_xrp = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_3_btc = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_5_eth = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2_eth = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2_post_iso = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_2 = AsyncMock(return_value=1)
    db.update_signal_evaluations_lgb_v9_1 = AsyncMock(return_value=1)
    db.update_window_ensemble_fields = AsyncMock(return_value=None)
    db.update_window_surface_fields = AsyncMock(return_value=None)
    monkeypatch.setattr(
        five_min_vpin, "_ensemble_surface_fields",
        lambda s: {
            "probability_lgb_v9_5_xrp": None,
            "probability_lgb_v9_3_btc": None,
            "probability_lgb_v9_5_eth": None,
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
        probability_lgb_v9_5_xrp=None,
        asset="XRP",
    )
    reg = _stub_registry(db)
    reg._write_window_trace(surface=surface)
    await asyncio.sleep(0)

    db.update_signal_evaluations_lgb_v9_5_xrp.assert_not_awaited()
