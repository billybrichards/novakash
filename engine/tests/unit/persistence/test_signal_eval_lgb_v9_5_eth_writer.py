"""Unit tests for the probability_lgb_v9_5_eth writer (DBClient + PgSignalRepo).

Sibling of test_signal_eval_lgb_v9_2_eth_writer.py. Coverage:
- DBClient.update_signal_evaluations_lgb_v9_5_eth upserts correctly
- No-ops when probability is None or eval_offset is None or pool is missing
- DB errors are swallowed (fire-and-forget)
- PgSignalRepository mirror has the same SQL contract
- update_window_ensemble_fields includes probability_lgb_v9_5_eth ($18)
- _ensemble_surface_fields propagates the v9_5_eth field
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from persistence.db_client import DBClient


# ── Fakes ─────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, result: str = "INSERT 0 1") -> None:
        self.calls: list[dict] = []
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


# ── DBClient.update_signal_evaluations_lgb_v9_5_eth ──────────────────────


@pytest.mark.asyncio
async def test_v9_5_eth_writer_upserts_row():
    db = _stub_db("INSERT 0 1")
    n = await db.update_signal_evaluations_lgb_v9_5_eth(
        window_ts=1777617300,
        asset="ETH",
        timeframe="5m",
        eval_offset=180,
        probability_lgb_v9_5_eth=0.965,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "probability_lgb_v9_5_eth" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "COALESCE" in sql
    args = call["args"]
    assert args[0] == 1777617300
    assert args[1] == "ETH"
    assert args[2] == "5m"
    assert args[3] == 180
    assert args[4] == pytest.approx(0.965)


@pytest.mark.asyncio
async def test_v9_5_eth_writer_noop_when_value_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_5_eth(
        window_ts=1777617300,
        asset="ETH",
        timeframe="5m",
        eval_offset=180,
        probability_lgb_v9_5_eth=None,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_v9_5_eth_writer_noop_when_eval_offset_none():
    db = _stub_db()
    n = await db.update_signal_evaluations_lgb_v9_5_eth(
        window_ts=1777617300,
        asset="ETH",
        timeframe="5m",
        eval_offset=None,
        probability_lgb_v9_5_eth=0.965,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_v9_5_eth_writer_noop_when_no_pool():
    db = DBClient.__new__(DBClient)
    db._pool = None
    n = await db.update_signal_evaluations_lgb_v9_5_eth(
        window_ts=1777617300,
        asset="ETH",
        timeframe="5m",
        eval_offset=180,
        probability_lgb_v9_5_eth=0.965,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_v9_5_eth_writer_swallows_db_errors():
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
    # Should not raise — fire-and-forget contract
    n = await db.update_signal_evaluations_lgb_v9_5_eth(
        window_ts=1777617300,
        asset="ETH",
        timeframe="5m",
        eval_offset=180,
        probability_lgb_v9_5_eth=0.965,
    )
    assert n == 0


# ── PgSignalRepository parity ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pg_signal_repo_v9_5_eth_writer_parity():
    """PgSignalRepository mirrors DBClient SQL contract."""
    from adapters.persistence.pg_signal_repo import PgSignalRepository

    pool = _FakePool("INSERT 0 1")
    repo = PgSignalRepository(pool)  # type: ignore[arg-type]
    n = await repo.update_signal_evaluations_lgb_v9_5_eth(
        window_ts=1777617300,
        asset="ETH",
        timeframe="5m",
        eval_offset=180,
        probability_lgb_v9_5_eth=0.965,
    )
    assert n == 1
    sql = pool.conn.calls[0]["sql"]
    assert "INSERT INTO signal_evaluations" in sql
    assert "probability_lgb_v9_5_eth" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "COALESCE" in sql


# ── _ensemble_surface_fields includes new column ──────────────────────────


class _SurfaceStubWithV9_5Eth:
    """Minimal FullDataSurface stand-in for _ensemble_surface_fields tests."""

    def __init__(
        self,
        *,
        probability_lgb_v9_5_eth: float | None = None,
        probability_lgb_v9_2_eth: float | None = None,
    ) -> None:
        self.window_ts = 1777617300
        self.asset = "ETH"
        self.timescale = "5m"
        self.eval_offset = 180
        self.assembled_at = 0.0
        self.probability_lgb_v9_2 = None
        self.probability_lgb_v9_2_post_iso = None
        self.probability_lgb_v9_2_eth = probability_lgb_v9_2_eth
        self.probability_lgb_v9_5_eth = probability_lgb_v9_5_eth
        self.probability_lgb_v12 = None
        self.probability_v2_meta_gate = None
        self.probability_v9_2_meta_gate = None
        self.probability_v12_meta_gate = None
        # minimal surface fields for _ensemble_surface_fields
        self.v2_probability_up = None
        self.probability_lgb = None
        self.probability_classifier = None
        self.ensemble_config = None


@pytest.mark.asyncio
async def test_ensemble_surface_fields_includes_v9_5_eth():
    """_ensemble_surface_fields passes probability_lgb_v9_5_eth when set."""
    from strategies.five_min_vpin import _ensemble_surface_fields

    surface = _SurfaceStubWithV9_5Eth(probability_lgb_v9_5_eth=0.965)
    fields = _ensemble_surface_fields(surface)
    assert "probability_lgb_v9_5_eth" in fields
    assert fields["probability_lgb_v9_5_eth"] == pytest.approx(0.965)


@pytest.mark.asyncio
async def test_ensemble_surface_fields_v9_5_eth_none_when_absent():
    """_ensemble_surface_fields returns None for v9_5_eth when absent."""
    from strategies.five_min_vpin import _ensemble_surface_fields

    surface = _SurfaceStubWithV9_5Eth(probability_lgb_v9_5_eth=None)
    fields = _ensemble_surface_fields(surface)
    assert "probability_lgb_v9_5_eth" in fields
    assert fields["probability_lgb_v9_5_eth"] is None


@pytest.mark.asyncio
async def test_ensemble_surface_fields_v9_5_and_v9_2_eth_independent():
    """Both fields are independently propagated — confirming side-by-side
    coexistence of v9.2 and v9.5 ETH probabilities on the surface."""
    from strategies.five_min_vpin import _ensemble_surface_fields

    surface = _SurfaceStubWithV9_5Eth(
        probability_lgb_v9_5_eth=0.965,
        probability_lgb_v9_2_eth=0.880,
    )
    fields = _ensemble_surface_fields(surface)
    assert fields["probability_lgb_v9_5_eth"] == pytest.approx(0.965)
    assert fields["probability_lgb_v9_2_eth"] == pytest.approx(0.880)


# ── update_window_ensemble_fields includes new column ($18) ──────────────


@pytest.mark.asyncio
async def test_update_window_ensemble_fields_includes_v9_5_eth():
    """window_snapshots upsert SQL contains probability_lgb_v9_5_eth as $18.

    The bind-parameter order after adding v9_5_eth:
      $1-$4 (window_ts/asset/tf/eval_offset)
      $5-$10 (ensemble_p_up, _lgb, _classifier, _mode, _disagreement, _model_version)
      $11-$13 (lgb_v12, lgb_v9_2, lgb_v9_2_post_iso)
      $14 (lgb_v9_2_eth)
      $15 (lgb_v9_5_eth)   <- NEW
      $16-$18 (v2_meta_gate, v9_2_meta_gate, v12_meta_gate)
    """
    db = _stub_db("INSERT 0 1")
    await db.update_window_ensemble_fields(
        window_ts=1777617300,
        asset="ETH",
        timeframe="5m",
        eval_offset=180,
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
            "probability_lgb_v9_2_eth": 0.880,
            "probability_lgb_v9_5_eth": 0.965,
            "probability_v2_meta_gate": None,
            "probability_v9_2_meta_gate": None,
            "probability_v12_meta_gate": None,
        },
    )
    sql = db._pool.conn.calls[0]["sql"]
    assert "probability_lgb_v9_5_eth" in sql
    assert "$18" in sql
    args = db._pool.conn.calls[0]["args"]
    # ETH v9.2 at index 13 (param $14), ETH v9.5 at index 14 (param $15)
    assert args[13] == pytest.approx(0.880)
    assert args[14] == pytest.approx(0.965)
