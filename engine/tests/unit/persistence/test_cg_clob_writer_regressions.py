"""Writer-regression tests for audit-tasks #337 and #338.

#337 — signal_evaluations CG fields (cg_funding_rate, cg_oi_delta_pct, etc.)
       were 100% NULL because _write_signal_evaluation in evaluate_strategies.py
       never populated the CG keys in its payload dict, and write_signal_evaluation
       in db_client / pg_signal_repo didn't include those columns in the INSERT.

#338 — window_snapshots clob_* columns (clob_up_bid, clob_up_ask, clob_down_bid,
       clob_down_ask, clob_imbalance, clob_implied_up, clob_fill_price) were 100%
       NULL because write_window_snapshot in both writers lacked those columns.

Fix family: same pattern as PR #445 (probability_lgb_v12 writer regression).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


# ─── shared mock plumbing ──────────────────────────────────────────────────

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


def _stub_db_client(result: str = "INSERT 0 1"):
    from persistence.db_client import DBClient

    db = DBClient.__new__(DBClient)
    db._pool = _FakePool(result)
    return db


def _stub_pg_repo(result: str = "INSERT 0 1"):
    from adapters.persistence.pg_signal_repo import PgSignalRepository

    return PgSignalRepository(_FakePool(result))  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
# #337 — signal_evaluations CG writer
# ═══════════════════════════════════════════════════════════════════════════

# ── DBClient.write_signal_evaluation ──────────────────────────────────────

@pytest.mark.asyncio
async def test_db_client_signal_eval_insert_includes_cg_columns():
    """SQL contract: write_signal_evaluation INSERT must list all 6 CG cols."""
    db = _stub_db_client()
    await db.write_signal_evaluation({
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "eval_offset": 60,
        "cg_funding_rate": 0.0001,
        "cg_oi_delta_pct": 0.02,
        "cg_liq_long_usd": 1_000_000.0,
        "cg_liq_short_usd": 500_000.0,
        "cg_taker_buy_usd": 2_000_000.0,
        "cg_taker_sell_usd": 1_800_000.0,
    })
    sql = db._pool.conn.calls[0]["sql"]
    for col in (
        "cg_funding_rate",
        "cg_oi_delta_pct",
        "cg_liq_long_usd",
        "cg_liq_short_usd",
        "cg_taker_buy_usd",
        "cg_taker_sell_usd",
    ):
        assert col in sql, f"INSERT missing column: {col}"


@pytest.mark.asyncio
async def test_db_client_signal_eval_cg_values_forwarded():
    """CG values from the payload dict must be forwarded as positional args."""
    db = _stub_db_client()
    await db.write_signal_evaluation({
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "eval_offset": 60,
        "cg_funding_rate": 0.0005,
        "cg_oi_delta_pct": 0.03,
        "cg_liq_long_usd": 100.0,
        "cg_liq_short_usd": 200.0,
        "cg_taker_buy_usd": 300.0,
        "cg_taker_sell_usd": 400.0,
    })
    args = db._pool.conn.calls[0]["args"]
    # The args are positional; we just verify the floats appear somewhere.
    assert 0.0005 in args or pytest.approx(0.0005) in [a for a in args if isinstance(a, float)]
    assert any(a == pytest.approx(100.0) for a in args if isinstance(a, float))


@pytest.mark.asyncio
async def test_db_client_signal_eval_cg_none_when_missing():
    """Missing CG keys → None args, no KeyError."""
    db = _stub_db_client()
    await db.write_signal_evaluation({
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "eval_offset": 60,
        # no cg_* keys at all
    })
    # Must not raise; at least one call was made
    assert len(db._pool.conn.calls) == 1


# ── PgSignalRepository.write_signal_evaluation parity ─────────────────────

@pytest.mark.asyncio
async def test_pg_repo_signal_eval_insert_includes_cg_columns():
    """pg_signal_repo SQL must match db_client — same CG columns."""
    repo = _stub_pg_repo()
    await repo.write_signal_evaluation({
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "eval_offset": 60,
        "cg_funding_rate": 0.0001,
        "cg_oi_delta_pct": 0.02,
        "cg_liq_long_usd": 1_000_000.0,
        "cg_liq_short_usd": 500_000.0,
        "cg_taker_buy_usd": 2_000_000.0,
        "cg_taker_sell_usd": 1_800_000.0,
    })
    sql = repo._pool.conn.calls[0]["sql"]
    for col in (
        "cg_funding_rate",
        "cg_oi_delta_pct",
        "cg_liq_long_usd",
        "cg_liq_short_usd",
        "cg_taker_buy_usd",
        "cg_taker_sell_usd",
    ):
        assert col in sql, f"pg_signal_repo INSERT missing column: {col}"


@pytest.mark.asyncio
async def test_pg_repo_signal_eval_parity_with_db_client():
    """Both writers must emit the same column set (drift prevention)."""
    db = _stub_db_client()
    repo = _stub_pg_repo()
    payload = {
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "eval_offset": 60,
        "cg_funding_rate": 0.0001,
        "cg_oi_delta_pct": 0.02,
        "cg_liq_long_usd": 1.0,
        "cg_liq_short_usd": 2.0,
        "cg_taker_buy_usd": 3.0,
        "cg_taker_sell_usd": 4.0,
    }
    await db.write_signal_evaluation(payload)
    await repo.write_signal_evaluation(payload)

    db_sql = db._pool.conn.calls[0]["sql"]
    repo_sql = repo._pool.conn.calls[0]["sql"]

    for col in (
        "cg_funding_rate",
        "cg_oi_delta_pct",
        "cg_liq_long_usd",
        "cg_liq_short_usd",
        "cg_taker_buy_usd",
        "cg_taker_sell_usd",
    ):
        assert col in db_sql, f"db_client missing: {col}"
        assert col in repo_sql, f"pg_signal_repo missing: {col}"


# ── evaluate_strategies._write_signal_evaluation payload ──────────────────

class _FakeCgSnapshot:
    oi_usd: float = 45_000_000_000.0
    oi_delta_pct_1m: float = 0.02
    liq_long_usd_1m: float = 1_200_000.0
    liq_short_usd_1m: float = 800_000.0
    liq_total_usd_1m: float = 2_000_000.0
    long_pct: float = 52.3
    short_pct: float = 47.7
    long_short_ratio: float = 1.1
    top_position_long_pct: float = 55.0
    top_position_short_pct: float = 45.0
    top_position_ratio: float = 1.22
    taker_buy_volume_1m: float = 5_000_000.0
    taker_sell_volume_1m: float = 4_500_000.0
    funding_rate: float = 0.0003
    funding_rate_annual: float = 0.0003 * 3 * 365
    connected: bool = True


class _FakeV4Snapshot:
    probability_up = 0.65
    probability_raw = 0.63
    regime = "NORMAL"
    regime_confidence = 0.8
    regime_persistence = 0.9
    conviction = "HIGH"
    conviction_score = 0.75
    sub_signals: dict = {}
    quantiles: dict = {}
    macro: dict = {}
    consensus: dict = {}
    polymarket_outcome = None


class _FakeCtx:
    asset = "BTC"
    window_ts = 1_777_617_300
    timeframe = "5m"
    eval_offset = 60
    current_price = 65_000.0
    open_price = 64_500.0
    delta_pct = 0.0077
    delta_binance = 0.0077
    delta_tiingo = 0.0075
    delta_chainlink = 0.0076
    delta_source = "tiingo_rest_candle"
    binance_price = 65_000.0
    vpin = 0.62
    regime = "NORMAL"
    twap_delta = 0.006
    tiingo_close = 65_010.0
    clob_up_bid = 0.72
    clob_up_ask = 0.73
    clob_down_bid = 0.27
    clob_down_ask = 0.28
    gamma_up_price = 0.72
    gamma_down_price = 0.28
    cg_snapshot = _FakeCgSnapshot()
    v4_snapshot = _FakeV4Snapshot()


class _FakeDecision:
    action = "TRADE"
    direction = "UP"
    skip_reason = None


@pytest.mark.asyncio
async def test_evaluate_strategies_payload_includes_cg_fields():
    """_write_signal_evaluation must include all CG fields when cg_snapshot is set."""
    from use_cases.evaluate_strategies import EvaluateStrategiesUseCase

    captured: list[dict] = []

    async def _fake_write(data: dict) -> None:
        captured.append(data)

    uc = EvaluateStrategiesUseCase.__new__(EvaluateStrategiesUseCase)
    uc._db = AsyncMock()
    uc._db.write_signal_evaluation = _fake_write
    uc._last_signal_eval_args = None

    ctx = _FakeCtx()
    decision = _FakeDecision()

    await uc._write_signal_evaluation(ctx, decision, "BTC", 1_777_617_300, 60, "5m")

    assert captured, "_write_signal_evaluation did not call db.write_signal_evaluation"
    payload = captured[0]

    assert payload["cg_funding_rate"] == pytest.approx(0.0003)
    assert payload["cg_oi_delta_pct"] == pytest.approx(0.02)
    assert payload["cg_liq_long_usd"] == pytest.approx(1_200_000.0)
    assert payload["cg_liq_short_usd"] == pytest.approx(800_000.0)
    assert payload["cg_taker_buy_usd"] == pytest.approx(5_000_000.0)
    assert payload["cg_taker_sell_usd"] == pytest.approx(4_500_000.0)


@pytest.mark.asyncio
async def test_evaluate_strategies_payload_cg_none_when_no_snapshot():
    """When cg_snapshot is None, CG payload fields must be None (no AttributeError)."""
    from use_cases.evaluate_strategies import EvaluateStrategiesUseCase

    captured: list[dict] = []

    async def _fake_write(data: dict) -> None:
        captured.append(data)

    uc = EvaluateStrategiesUseCase.__new__(EvaluateStrategiesUseCase)
    uc._db = AsyncMock()
    uc._db.write_signal_evaluation = _fake_write
    uc._last_signal_eval_args = None

    ctx = _FakeCtx()
    ctx.cg_snapshot = None  # type: ignore[assignment]
    decision = _FakeDecision()

    await uc._write_signal_evaluation(ctx, decision, "BTC", 1_777_617_300, 60, "5m")

    assert captured
    payload = captured[0]
    for key in ("cg_funding_rate", "cg_oi_delta_pct", "cg_liq_long_usd",
                "cg_liq_short_usd", "cg_taker_buy_usd", "cg_taker_sell_usd"):
        assert payload.get(key) is None, f"{key} should be None when cg_snapshot is None"


# ═══════════════════════════════════════════════════════════════════════════
# #338 — window_snapshots clob_* writer
# ═══════════════════════════════════════════════════════════════════════════

_CLOB_COLS = (
    "clob_up_bid",
    "clob_up_ask",
    "clob_down_bid",
    "clob_down_ask",
    "clob_imbalance",
    "clob_implied_up",
    "clob_fill_price",
)


# ── DBClient.write_window_snapshot ────────────────────────────────────────

@pytest.mark.asyncio
async def test_db_client_window_snapshot_insert_includes_clob_columns():
    """SQL contract: write_window_snapshot INSERT must list all 7 clob_* cols."""
    db = _stub_db_client()
    await db.write_window_snapshot({
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "clob_up_bid": 0.72,
        "clob_up_ask": 0.73,
        "clob_down_bid": 0.27,
        "clob_down_ask": 0.28,
        "clob_imbalance": 0.05,
        "clob_implied_up": 0.72,
        "clob_fill_price": 0.73,
    })
    sql = db._pool.conn.calls[0]["sql"]
    for col in _CLOB_COLS:
        assert col in sql, f"db_client write_window_snapshot INSERT missing: {col}"


@pytest.mark.asyncio
async def test_db_client_window_snapshot_clob_on_conflict_coalesce():
    """ON CONFLICT clause must use COALESCE for clob_* (idempotency)."""
    db = _stub_db_client()
    await db.write_window_snapshot({
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "clob_up_bid": 0.72,
        "clob_up_ask": 0.73,
        "clob_down_bid": 0.27,
        "clob_down_ask": 0.28,
    })
    sql = db._pool.conn.calls[0]["sql"]
    assert "ON CONFLICT" in sql
    # At minimum the 4 primary CLOB columns must appear in the COALESCE DO UPDATE
    for col in ("clob_up_bid", "clob_up_ask", "clob_down_bid", "clob_down_ask"):
        assert f"COALESCE(EXCLUDED.{col}" in sql, (
            f"ON CONFLICT DO UPDATE must COALESCE {col}"
        )


@pytest.mark.asyncio
async def test_db_client_window_snapshot_clob_values_forwarded():
    """clob_* values from snapshot dict must appear as positional args."""
    db = _stub_db_client()
    await db.write_window_snapshot({
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "clob_up_bid": 0.72,
        "clob_up_ask": 0.73,
        "clob_down_bid": 0.27,
        "clob_down_ask": 0.28,
    })
    args = db._pool.conn.calls[0]["args"]
    float_args = [a for a in args if isinstance(a, float)]
    assert pytest.approx(0.72) in float_args
    assert pytest.approx(0.73) in float_args
    assert pytest.approx(0.27) in float_args
    assert pytest.approx(0.28) in float_args


# ── PgSignalRepository.write_window_snapshot parity ───────────────────────

@pytest.mark.asyncio
async def test_pg_repo_window_snapshot_insert_includes_clob_columns():
    """pg_signal_repo SQL must match db_client — same clob_* columns."""
    repo = _stub_pg_repo()
    await repo.write_window_snapshot({
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "clob_up_bid": 0.72,
        "clob_up_ask": 0.73,
        "clob_down_bid": 0.27,
        "clob_down_ask": 0.28,
        "clob_imbalance": 0.05,
        "clob_implied_up": 0.72,
        "clob_fill_price": 0.73,
    })
    sql = repo._pool.conn.calls[0]["sql"]
    for col in _CLOB_COLS:
        assert col in sql, f"pg_signal_repo write_window_snapshot INSERT missing: {col}"


@pytest.mark.asyncio
async def test_pg_repo_window_snapshot_parity_with_db_client():
    """Both writers must emit the same clob_* column set (drift prevention)."""
    db = _stub_db_client()
    repo = _stub_pg_repo()
    snapshot = {
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "clob_up_bid": 0.72,
        "clob_up_ask": 0.73,
        "clob_down_bid": 0.27,
        "clob_down_ask": 0.28,
        "clob_imbalance": 0.05,
        "clob_implied_up": 0.72,
        "clob_fill_price": 0.73,
    }
    await db.write_window_snapshot(snapshot)
    await repo.write_window_snapshot(snapshot)

    db_sql = db._pool.conn.calls[0]["sql"]
    repo_sql = repo._pool.conn.calls[0]["sql"]

    for col in _CLOB_COLS:
        assert col in db_sql, f"db_client missing: {col}"
        assert col in repo_sql, f"pg_signal_repo missing: {col}"


@pytest.mark.asyncio
async def test_pg_repo_window_snapshot_on_conflict_coalesce():
    """pg_signal_repo ON CONFLICT must also COALESCE clob_* columns."""
    repo = _stub_pg_repo()
    await repo.write_window_snapshot({
        "window_ts": 1_777_617_300,
        "asset": "BTC",
        "timeframe": "5m",
        "clob_up_bid": 0.72,
    })
    sql = repo._pool.conn.calls[0]["sql"]
    assert "ON CONFLICT" in sql
    assert "COALESCE(EXCLUDED.clob_up_bid" in sql
