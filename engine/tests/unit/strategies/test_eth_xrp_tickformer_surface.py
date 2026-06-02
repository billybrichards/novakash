"""Tests for ETH/XRP per-asset TickFormer surface assembly.

Covers the fix introduced by fix/eth-xrp-tickformer-column-pipeline
(RDS note #833):

  1. The 8 per-asset keys are now in _TF_KEYS so the tickformer sub-cache
     carries them — non-BTC assets never trigger the poly guard, so the
     main cache always wins, but having them in the subcache protects
     against any future guard added for non-BTC.

  2. get_surface reads the ETH/XRP fields from ts_data (main path) AND
     falls back to _tf_subcache (safety-net path) — mirrors the v20 pattern.

  3. A tickformer_v16_pure_eth strategy hook reads
     ``surface.probability_tickformer_v16_eth`` via the shared
     evaluate_tickformer_strategy base and returns TRADE/SKIP (not
     ``tickformer_v16_eth_not_available``) when the field is populated.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.data_surface import DataSurfaceManager, FullDataSurface


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mgr() -> DataSurfaceManager:
    return DataSurfaceManager(
        v4_base_url="http://fake",
        active_assets=["BTC", "ETH", "XRP"],
    )


class _Window:
    def __init__(self, asset: str = "ETH", timeframe: str = "5m") -> None:
        self.asset = asset
        self.timeframe = timeframe
        self.window_ts = 1779500000
        self.open_price = 3_000.0
        self.eval_offset = 120
        self.up_price = 0.60
        self.down_price = 0.40


def _eth_body(
    *,
    v16: float = 0.87,
    v17: float = 0.91,
    v18: float = 0.84,
    v20: Optional[float] = 0.90,
) -> dict:
    """Non-BTC (ETH) /v4/snapshot body with per-asset tickformer fields."""
    return {
        "ts": time.time(),
        "status": "no_model",
        "asset": "ETH",
        "timescales": {
            "5m": {
                "probability_classifier": 0.71,
                "regime": "calm_trend",
                "probability_tickformer_v16_eth": v16,
                "probability_tickformer_v17_eth": v17,
                "probability_tickformer_v18_eth": v18,
                "probability_tickformer_v20_eth": v20,
            },
            "15m": {},
        },
    }


def _xrp_body(
    *,
    v16: float = 0.82,
    v17: float = 0.88,
    v18: float = 0.79,
    v20: Optional[float] = 0.85,
) -> dict:
    """Non-BTC (XRP) /v4/snapshot body with per-asset tickformer fields."""
    return {
        "ts": time.time(),
        "status": "no_model",
        "asset": "XRP",
        "timescales": {
            "5m": {
                "probability_classifier": 0.65,
                "regime": "calm_trend",
                "probability_tickformer_v16_xrp": v16,
                "probability_tickformer_v17_xrp": v17,
                "probability_tickformer_v18_xrp": v18,
                "probability_tickformer_v20_xrp": v20,
            },
            "15m": {},
        },
    }


def _minimal_surface(
    *,
    asset: str = "ETH",
    v16_eth: Optional[float] = None,
    v17_eth: Optional[float] = None,
    v18_eth: Optional[float] = None,
    v20_eth: Optional[float] = None,
    v16_xrp: Optional[float] = None,
    v17_xrp: Optional[float] = None,
    v18_xrp: Optional[float] = None,
    v20_xrp: Optional[float] = None,
) -> FullDataSurface:
    """Build a minimal FullDataSurface with only the fields under test populated."""
    return FullDataSurface(
        asset=asset,
        timescale="5m",
        window_ts=1779500000,
        eval_offset=120,
        assembled_at=time.time(),
        current_price=3000.0,
        open_price=3000.0,
        delta_binance=None,
        delta_tiingo=None,
        delta_chainlink=None,
        delta_pct=0.0,
        delta_source="unknown",
        vpin=0.3,
        regime="CALM",
        twap_delta=None,
        v2_probability_up=None,
        v2_probability_raw=None,
        v2_quantiles_p10=None,
        v2_quantiles_p50=None,
        v2_quantiles_p90=None,
        probability_lgb=None,
        probability_classifier=None,
        ensemble_config=None,
        v3_5m_composite=None,
        v3_15m_composite=None,
        v3_1h_composite=None,
        v3_4h_composite=None,
        v3_24h_composite=None,
        v3_48h_composite=None,
        v3_72h_composite=None,
        v3_1w_composite=None,
        v3_2w_composite=None,
        v3_sub_elm=None,
        v3_sub_cascade=None,
        v3_sub_taker=None,
        v3_sub_oi=None,
        v3_sub_funding=None,
        v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime=None,
        v4_regime_confidence=None,
        v4_regime_persistence=None,
        v4_macro_bias=None,
        v4_macro_direction_gate=None,
        v4_macro_size_modifier=None,
        v4_consensus_safe_to_trade=None,
        v4_consensus_agreement_score=None,
        v4_consensus_max_divergence_bps=None,
        v4_conviction=None,
        v4_conviction_score=None,
        poly_direction=None,
        poly_trade_advised=None,
        poly_confidence=None,
        poly_confidence_distance=None,
        poly_timing=None,
        poly_max_entry_price=None,
        poly_reason=None,
        v4_recommended_side=None,
        v4_recommended_collateral_pct=None,
        v4_sub_signals=None,
        v4_quantiles=None,
        clob_up_bid=0.63,
        clob_up_ask=0.65,
        clob_down_bid=0.35,
        clob_down_ask=0.37,
        clob_implied_up=0.64,
        gamma_up_price=None,
        gamma_down_price=None,
        cg_oi_usd=None,
        cg_funding_rate=None,
        cg_taker_buy_vol=None,
        cg_taker_sell_vol=None,
        cg_liq_total=None,
        cg_liq_long=None,
        cg_liq_short=None,
        cg_long_short_ratio=None,
        timesfm_expected_move_bps=None,
        timesfm_vol_forecast_bps=None,
        hour_utc=12,
        seconds_to_close=120,
        # ETH/XRP tickformer heads under test
        probability_tickformer_v16_eth=v16_eth,
        probability_tickformer_v17_eth=v17_eth,
        probability_tickformer_v18_eth=v18_eth,
        probability_tickformer_v20_eth=v20_eth,
        probability_tickformer_v16_xrp=v16_xrp,
        probability_tickformer_v17_xrp=v17_xrp,
        probability_tickformer_v18_xrp=v18_xrp,
        probability_tickformer_v20_xrp=v20_xrp,
    )


# ── 1. Surface assembly — main cache path ───────────────────────────────────


def test_eth_tickformer_fields_read_from_main_cache():
    """ETH snapshot carries per-asset fields; get_surface populates them."""
    mgr = _mgr()
    body = _eth_body(v16=0.87, v17=0.91, v18=0.84, v20=0.90)
    # Simulate main cache populated (non-BTC poly guard never fires for ETH)
    mgr._cached_v4["ETH"] = body
    mgr._cached_v4_ts["ETH"] = time.time()

    surface = mgr.get_surface(_Window(asset="ETH"), 120)

    assert surface.probability_tickformer_v16_eth == pytest.approx(0.87)
    assert surface.probability_tickformer_v17_eth == pytest.approx(0.91)
    assert surface.probability_tickformer_v18_eth == pytest.approx(0.84)
    assert surface.probability_tickformer_v20_eth == pytest.approx(0.90)
    # XRP fields should be None on an ETH surface
    assert surface.probability_tickformer_v16_xrp is None
    assert surface.probability_tickformer_v20_xrp is None


def test_xrp_tickformer_fields_read_from_main_cache():
    """XRP snapshot carries per-asset fields; get_surface populates them."""
    mgr = _mgr()
    body = _xrp_body(v16=0.82, v17=0.88, v18=0.79, v20=0.85)
    mgr._cached_v4["XRP"] = body
    mgr._cached_v4_ts["XRP"] = time.time()

    surface = mgr.get_surface(_Window(asset="XRP"), 90)

    assert surface.probability_tickformer_v16_xrp == pytest.approx(0.82)
    assert surface.probability_tickformer_v17_xrp == pytest.approx(0.88)
    assert surface.probability_tickformer_v18_xrp == pytest.approx(0.79)
    assert surface.probability_tickformer_v20_xrp == pytest.approx(0.85)
    # ETH fields should be None on an XRP surface
    assert surface.probability_tickformer_v16_eth is None
    assert surface.probability_tickformer_v20_eth is None


def test_eth_xrp_fields_none_when_main_cache_empty():
    """When no v4 cache entry exists for the asset, ETH/XRP fields are None."""
    mgr = _mgr()
    # No cache populated for ETH at all
    surface = mgr.get_surface(_Window(asset="ETH"), 120)
    assert surface.probability_tickformer_v16_eth is None
    assert surface.probability_tickformer_v17_eth is None
    assert surface.probability_tickformer_v18_eth is None
    assert surface.probability_tickformer_v20_eth is None


# ── 2. Surface assembly — subcache fallback path ────────────────────────────


def test_eth_tickformer_fields_from_subcache_when_main_cache_stale():
    """When main cache is stale (>60s), ETH fields fall back to subcache."""
    mgr = _mgr()
    # Main cache is stale (61s old)
    body = _eth_body(v16=0.80)
    mgr._cached_v4["ETH"] = body
    mgr._cached_v4_ts["ETH"] = time.time() - 61

    # Subcache is fresh with newer values
    mgr._cached_tickformer["ETH"] = {
        "probability_tickformer_v16_eth": 0.89,
        "probability_tickformer_v18_eth": 0.86,
    }
    mgr._cached_tickformer_ts["ETH"] = time.time()

    surface = mgr.get_surface(_Window(asset="ETH"), 120)

    # Stale main cache is rejected; subcache values flow through
    assert surface.probability_tickformer_v16_eth == pytest.approx(0.89)
    assert surface.probability_tickformer_v18_eth == pytest.approx(0.86)
    # v17 not in subcache → None
    assert surface.probability_tickformer_v17_eth is None


def test_eth_tickformer_subcache_keys_present_in_tf_keys():
    """All 8 ETH/XRP keys are present in the _TF_KEYS tuple used by _try_fetch_snapshot.

    This test guards against the original bug: the subcache parsing loop
    only extracted BTC keys, silently discarding per-asset ETH/XRP values
    from the /v4/snapshot timescales.5m block.
    """
    from strategies import data_surface as ds_module
    import inspect

    # Read _TF_KEYS from the source. The tuple is defined as a local in
    # _try_fetch_snapshot; we extract it by running the same logic.
    src = inspect.getsource(ds_module.DataSurfaceManager._try_fetch_snapshot)
    eth_xrp_keys = (
        "probability_tickformer_v16_eth",
        "probability_tickformer_v17_eth",
        "probability_tickformer_v18_eth",
        "probability_tickformer_v20_eth",
        "probability_tickformer_v16_xrp",
        "probability_tickformer_v17_xrp",
        "probability_tickformer_v18_xrp",
        "probability_tickformer_v20_xrp",
    )
    for key in eth_xrp_keys:
        assert key in src, (
            f"Key {key!r} not found in _try_fetch_snapshot source — "
            "subcache will silently drop ETH/XRP tickformer values"
        )


# ── 3. Strategy hook reads the correct surface field ────────────────────────


def test_tickformer_v16_pure_eth_hook_reads_eth_field():
    """evaluate_tickformer_v16_pure_eth returns SKIP(shadow) not not_available when field populated."""
    from strategies.configs.tickformer_v16_pure_eth import evaluate_tickformer_v16_pure_eth
    import strategies.gate_params as _gp

    surface = _minimal_surface(asset="ETH", v16_eth=0.91)

    # Inject minimal gate_params for the eval via the public API
    token = _gp.set_active({
        "shadow_only": 1,
        "up_threshold": 0.84,
        "down_threshold": 0.16,
        "eval_offset_remaining_min": 60,
        "eval_offset_remaining_max": 240,
        "entry_cap": 0.93,
        "entry_floor_up": 0.50,
        "entry_cap_down": 0.90,
        "entry_cap_up": 1.0,
        "collateral_pct": 0.025,
        "gtc_cap": 0.96,
    })
    try:
        decision = evaluate_tickformer_v16_pure_eth(surface)
    finally:
        _gp.reset_active(token)

    # Must NOT be not_available (field IS populated)
    assert decision.skip_reason != "tickformer_v16_eth_not_available", (
        f"Strategy returned not_available despite v16_eth=0.91 on surface. "
        f"skip_reason={decision.skip_reason!r}"
    )
    # shadow_only=1 → should be shadow_only_no_trade (or direction-skip), never not_available
    assert "not_available" not in (decision.skip_reason or "")


def test_tickformer_v16_pure_eth_hook_returns_not_available_when_field_none():
    """evaluate_tickformer_v16_pure_eth returns not_available when v16_eth is None."""
    from strategies.configs.tickformer_v16_pure_eth import evaluate_tickformer_v16_pure_eth
    import strategies.gate_params as _gp

    surface = _minimal_surface(asset="ETH", v16_eth=None)

    token = _gp.set_active({
        "shadow_only": 1,
        "up_threshold": 0.84,
        "down_threshold": 0.16,
        "eval_offset_remaining_min": 60,
        "eval_offset_remaining_max": 240,
    })
    try:
        decision = evaluate_tickformer_v16_pure_eth(surface)
    finally:
        _gp.reset_active(token)

    assert decision.skip_reason == "tickformer_v16_eth_not_available"


# ── 4. Writer tests ─────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, result: str = "INSERT 0 1") -> None:
        self.calls: list[dict] = []
        self._result = result

    async def execute(self, sql: str, *args, **kwargs):
        self.calls.append({"sql": sql, "args": args})
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


def _stub_db():
    from persistence.db_client import DBClient
    db = DBClient.__new__(DBClient)
    db._pool = _FakePool()
    return db


@pytest.mark.asyncio
async def test_db_client_eth_xrp_writer_upserts_all_8_columns():
    """update_signal_evaluations_tickformer_eth_xrp writes all 8 columns."""
    db = _stub_db()
    n = await db.update_signal_evaluations_tickformer_eth_xrp(
        window_ts=1779500000,
        asset="ETH",
        timeframe="5m",
        eval_offset=120,
        probability_tickformer_v16_eth=0.87,
        probability_tickformer_v17_eth=0.91,
        probability_tickformer_v18_eth=0.84,
        probability_tickformer_v20_eth=0.90,
        probability_tickformer_v16_xrp=0.82,
        probability_tickformer_v17_xrp=0.88,
        probability_tickformer_v18_xrp=0.79,
        probability_tickformer_v20_xrp=0.85,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    sql = call["sql"]

    assert "INSERT INTO signal_evaluations" in sql
    assert "ON CONFLICT (window_ts, asset, timeframe, eval_offset)" in sql
    assert "DO UPDATE SET" in sql

    for col in (
        "probability_tickformer_v16_eth",
        "probability_tickformer_v17_eth",
        "probability_tickformer_v18_eth",
        "probability_tickformer_v20_eth",
        "probability_tickformer_v16_xrp",
        "probability_tickformer_v17_xrp",
        "probability_tickformer_v18_xrp",
        "probability_tickformer_v20_xrp",
    ):
        assert col in sql, f"column {col!r} missing from SQL"
        flat = sql.replace("\n", " ").replace("  ", " ")
        assert f"COALESCE(\n                            signal_evaluations.{col}" in sql or \
               f"COALESCE( signal_evaluations.{col}" in flat or \
               f"COALESCE(signal_evaluations.{col}" in flat.replace("  ", ""), \
               f"COALESCE not found for {col!r}"

    args = call["args"]
    assert args[0] == 1779500000
    assert args[1] == "ETH"
    assert args[2] == "5m"
    assert args[3] == 120
    assert args[4] == pytest.approx(0.87)   # v16_eth
    assert args[5] == pytest.approx(0.91)   # v17_eth
    assert args[6] == pytest.approx(0.84)   # v18_eth
    assert args[7] == pytest.approx(0.90)   # v20_eth
    assert args[8] == pytest.approx(0.82)   # v16_xrp
    assert args[9] == pytest.approx(0.88)   # v17_xrp
    assert args[10] == pytest.approx(0.79)  # v18_xrp
    assert args[11] == pytest.approx(0.85)  # v20_xrp


@pytest.mark.asyncio
async def test_db_client_eth_xrp_writer_noop_when_all_none():
    """No SQL when all 8 columns are None."""
    db = _stub_db()
    n = await db.update_signal_evaluations_tickformer_eth_xrp(
        window_ts=1779500000, asset="ETH", timeframe="5m", eval_offset=120,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_db_client_eth_xrp_writer_noop_when_eval_offset_none():
    """eval_offset is part of the unique key; None → no SQL issued."""
    db = _stub_db()
    n = await db.update_signal_evaluations_tickformer_eth_xrp(
        window_ts=1779500000, asset="ETH", timeframe="5m", eval_offset=None,
        probability_tickformer_v16_eth=0.87,
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_db_client_eth_xrp_writer_partial_columns():
    """Writer fires when only ETH columns are populated (XRP all None)."""
    db = _stub_db()
    n = await db.update_signal_evaluations_tickformer_eth_xrp(
        window_ts=1779500000, asset="ETH", timeframe="5m", eval_offset=120,
        probability_tickformer_v18_eth=0.84,
    )
    assert n == 1
    call = db._pool.conn.calls[0]
    args = call["args"]
    assert args[4] is None    # v16_eth
    assert args[5] is None    # v17_eth
    assert args[6] == pytest.approx(0.84)  # v18_eth
    assert args[7] is None    # v20_eth
    assert args[8] is None    # v16_xrp
    assert args[11] is None   # v20_xrp


@pytest.mark.asyncio
async def test_pg_signal_repo_eth_xrp_writer_upserts_all_8_columns():
    """PgSignalRepo.update_signal_evaluations_tickformer_eth_xrp mirrors DBClient."""
    from adapters.persistence.pg_signal_repo import PgSignalRepository as PgSignalRepo

    repo = PgSignalRepo.__new__(PgSignalRepo)
    repo._pool = _FakePool()

    n = await repo.update_signal_evaluations_tickformer_eth_xrp(
        window_ts=1779500000,
        asset="XRP",
        timeframe="5m",
        eval_offset=90,
        probability_tickformer_v16_xrp=0.82,
        probability_tickformer_v20_xrp=0.85,
    )
    assert n == 1
    call = repo._pool.conn.calls[0]
    sql = call["sql"]
    assert "probability_tickformer_v16_xrp" in sql
    assert "probability_tickformer_v20_xrp" in sql
    # ETH fields should still be in the SQL template (always present)
    assert "probability_tickformer_v16_eth" in sql

    args = call["args"]
    assert args[0] == 1779500000
    assert args[1] == "XRP"
    assert args[3] == 90
    assert args[4] is None    # v16_eth (not passed)
    assert args[8] == pytest.approx(0.82)   # v16_xrp
    assert args[11] == pytest.approx(0.85)  # v20_xrp


# ── 5. Migration SQL contains all 8 columns ─────────────────────────────────


def test_migration_sql_contains_all_8_eth_xrp_columns():
    """The migration file declares ADD COLUMN IF NOT EXISTS for all 8 columns."""
    migration_path = (
        Path(__file__).resolve().parents[4]
        / "migrations"
        / "add_tickformer_eth_xrp_columns.sql"
    )
    assert migration_path.exists(), f"Migration file not found: {migration_path}"
    sql = migration_path.read_text()
    expected_cols = (
        "probability_tickformer_v16_eth",
        "probability_tickformer_v17_eth",
        "probability_tickformer_v18_eth",
        "probability_tickformer_v20_eth",
        "probability_tickformer_v16_xrp",
        "probability_tickformer_v17_xrp",
        "probability_tickformer_v18_xrp",
        "probability_tickformer_v20_xrp",
    )
    for col in expected_cols:
        assert col in sql, f"Column {col!r} missing from migration SQL"
    assert "ADD COLUMN IF NOT EXISTS" in sql
