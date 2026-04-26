"""Tests for DataSurfaceManager.warmup_from_db (cold-start seeding).

After every engine restart, the Tiingo + Chainlink delta caches don't
populate the in-memory surface for ~5–6 minutes while ticks fan out
through the data-surface pipeline. ``warmup_from_db`` queries the most
recent ``signal_evaluations`` row at startup so strategies have usable
(slightly stale) values from tick 0.

These tests use the same MockConnection/MockPool pattern as
``test_pg_window_state_repo.py`` to avoid a real DB.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest


class MockConnection:
    def __init__(self) -> None:
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetchrow_result: Optional[dict] = None
        self.fetchrow_exception: Optional[Exception] = None
        # Per-asset queue: key=asset, value=row to return for that asset.
        self.fetchrow_by_asset: dict[str, Optional[dict]] = {}

    async def fetchrow(self, q: str, *a: Any) -> Any:
        self.fetchrow_calls.append((q, a))
        if self.fetchrow_exception is not None:
            raise self.fetchrow_exception
        if self.fetchrow_by_asset and a:
            return self.fetchrow_by_asset.get(a[0])
        return self.fetchrow_result


class MockPool:
    def __init__(self, conn: MockConnection) -> None:
        self._conn = conn

    def acquire(self) -> "_Ctx":
        return _Ctx(self._conn)


class _Ctx:
    def __init__(self, c: MockConnection) -> None:
        self._c = c

    async def __aenter__(self) -> MockConnection:
        return self._c

    async def __aexit__(self, *a: Any) -> None:
        pass


def _row(
    asset: str = "BTC",
    minutes_ago: float = 1.0,
    delta_chainlink: Optional[float] = 0.0012,
    delta_tiingo: Optional[float] = 0.0011,
    delta_binance: Optional[float] = 0.0013,
    delta_source: str = "chainlink",
    vpin: float = 0.42,
    regime: str = "NORMAL",
    clob_up_bid: float = 0.55,
    clob_up_ask: float = 0.57,
    clob_down_bid: float = 0.43,
    clob_down_ask: float = 0.45,
) -> dict:
    """Build a fake signal_evaluations row dict, matching asyncpg.Record access."""
    evaluated_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "window_ts": 1781737200,
        "asset": asset,
        "timeframe": "5m",
        "eval_offset": -90,
        "clob_up_bid": clob_up_bid,
        "clob_up_ask": clob_up_ask,
        "clob_down_bid": clob_down_bid,
        "clob_down_ask": clob_down_ask,
        "binance_price": 95000.0,
        "chainlink_price": 95001.0,
        "tiingo_close": 95002.0,
        "delta_pct": delta_chainlink if delta_chainlink is not None else 0.0,
        "delta_tiingo": delta_tiingo,
        "delta_binance": delta_binance,
        "delta_chainlink": delta_chainlink,
        "delta_source": delta_source,
        "vpin": vpin,
        "regime": regime,
        "v2_probability_up": 0.62,
        "v2_model_version": "test",
        "evaluated_at": evaluated_at,
    }


@pytest.fixture
def conn() -> MockConnection:
    return MockConnection()


@pytest.fixture
def pool(conn: MockConnection) -> MockPool:
    return MockPool(conn)


@pytest.fixture
def mgr() -> Any:
    """Return a DataSurfaceManager instance with no feeds wired."""
    from strategies.data_surface import DataSurfaceManager

    return DataSurfaceManager(active_assets=["BTC"])


class TestWarmupHappyPath:
    def test_recent_row_seeds_cache(self, mgr: Any, pool: MockPool, conn: MockConnection) -> None:
        conn.fetchrow_result = _row(minutes_ago=1.5)
        asyncio.run(mgr.warmup_from_db(pool))

        assert "BTC" in mgr._warmup_cache
        seeded = mgr._warmup_cache["BTC"]
        assert seeded["delta_chainlink"] == pytest.approx(0.0012)
        assert seeded["delta_tiingo"] == pytest.approx(0.0011)
        assert seeded["vpin"] == pytest.approx(0.42)
        assert seeded["regime"] == "NORMAL"
        assert seeded["clob_up_bid"] == pytest.approx(0.55)

        # Query was actually issued.
        assert len(conn.fetchrow_calls) == 1
        sql, args = conn.fetchrow_calls[0]
        assert "FROM signal_evaluations" in sql
        assert "INTERVAL '15 minutes'" in sql
        assert args[0] == "BTC"

    def test_warmup_used_as_fallback_when_feeds_empty(
        self, mgr: Any, pool: MockPool, conn: MockConnection
    ) -> None:
        conn.fetchrow_result = _row(minutes_ago=2.0)
        asyncio.run(mgr.warmup_from_db(pool))

        # Build a minimal window object; no feeds wired, so live reads
        # return None — warmup MUST fill in deltas + vpin + clob.
        class W:
            asset = "BTC"
            window_ts = 1781737200
            timeframe = "5m"
            duration_secs = 300
            open_price = 95000.0

        surface = mgr.get_surface(W(), eval_offset=-90)
        assert surface.delta_chainlink == pytest.approx(0.0012)
        assert surface.delta_tiingo == pytest.approx(0.0011)
        assert surface.delta_binance == pytest.approx(0.0013)
        assert surface.vpin == pytest.approx(0.42)
        assert surface.regime == "NORMAL"
        assert surface.clob_up_bid == pytest.approx(0.55)
        assert surface.clob_down_ask == pytest.approx(0.45)


class TestWarmupNoRecentRow:
    def test_empty_db_logs_and_returns(
        self, mgr: Any, pool: MockPool, conn: MockConnection
    ) -> None:
        conn.fetchrow_result = None
        asyncio.run(mgr.warmup_from_db(pool))

        assert mgr._warmup_cache == {}
        # Query was attempted but returned no row — surface untouched.
        assert len(conn.fetchrow_calls) == 1


class TestWarmupStaleFilter:
    def test_row_outside_15min_not_used(
        self, mgr: Any, pool: MockPool, conn: MockConnection
    ) -> None:
        # Warmup-stored row: simulate a row written but stamped 20min ago.
        # The DB-side filter (INTERVAL '15 minutes') would normally drop
        # this row — we test the in-memory ``_get_warmup`` freshness gate
        # by force-seeding an old row directly.
        old_row = _row(minutes_ago=20.0)
        mgr._warmup_cache["BTC"] = old_row

        # Live feeds empty — without freshness filter we'd see warmup data.
        class W:
            asset = "BTC"
            window_ts = 1781737200
            timeframe = "5m"
            duration_secs = 300
            open_price = 95000.0

        surface = mgr.get_surface(W(), eval_offset=-90)
        # Stale warmup must be ignored — live reads were None, so deltas
        # stay None / 0.0.
        assert surface.delta_chainlink is None
        assert surface.delta_tiingo is None
        assert surface.regime == "UNKNOWN"
        assert surface.clob_up_bid is None


class TestWarmupFailOpen:
    def test_db_query_exception_is_swallowed(
        self, mgr: Any, pool: MockPool, conn: MockConnection
    ) -> None:
        conn.fetchrow_exception = RuntimeError("boom")
        # Must NOT raise — engine startup must not block on warmup.
        asyncio.run(mgr.warmup_from_db(pool))
        assert mgr._warmup_cache == {}

    def test_none_pool_is_safe(self, mgr: Any) -> None:
        # Pool unavailable (DB hadn't connected yet) — warmup is a no-op.
        asyncio.run(mgr.warmup_from_db(None))
        assert mgr._warmup_cache == {}


class TestWarmupMultiAsset:
    def test_only_seeds_active_assets(self, pool: MockPool, conn: MockConnection) -> None:
        from strategies.data_surface import DataSurfaceManager

        mgr_multi = DataSurfaceManager(active_assets=["BTC", "ETH"])
        # Per-asset rows with different delta_source values so we can tell
        # them apart.
        conn.fetchrow_by_asset = {
            "BTC": _row(asset="BTC", minutes_ago=1.0, delta_source="chainlink"),
            "ETH": _row(asset="ETH", minutes_ago=2.0, delta_source="tiingo_rest_candle"),
        }
        asyncio.run(mgr_multi.warmup_from_db(pool))

        assert set(mgr_multi._warmup_cache.keys()) == {"BTC", "ETH"}
        assert mgr_multi._warmup_cache["BTC"]["delta_source"] == "chainlink"
        assert (
            mgr_multi._warmup_cache["ETH"]["delta_source"] == "tiingo_rest_candle"
        )
        # SOL was not in active_assets — must not be queried.
        queried_assets = [args[0] for _, args in conn.fetchrow_calls]
        assert set(queried_assets) == {"BTC", "ETH"}
        assert "SOL" not in queried_assets


class TestLiveValuesWin:
    def test_live_chainlink_overrides_warmup(
        self, mgr: Any, pool: MockPool, conn: MockConnection
    ) -> None:
        """Live feed values must take precedence over warmup fallback."""
        conn.fetchrow_result = _row(minutes_ago=1.0, delta_chainlink=0.005)
        asyncio.run(mgr.warmup_from_db(pool))

        # Wire a fake chainlink feed with a fresher price → produces a
        # different delta than the warmup row.
        class FakeChainlink:
            latest_prices = {"BTC": 95500.0}  # vs open_price 95000 → +0.0053

        mgr._chainlink = FakeChainlink()

        class W:
            asset = "BTC"
            window_ts = 1781737200
            timeframe = "5m"
            duration_secs = 300
            open_price = 95000.0

        surface = mgr.get_surface(W(), eval_offset=-90)
        # Live feed should win — delta computed from live price, not warmup.
        live_delta = (95500.0 - 95000.0) / 95000.0
        assert surface.delta_chainlink == pytest.approx(live_delta)
        # Sanity: live differs from warmup, otherwise the test is vacuous.
        assert abs(surface.delta_chainlink - 0.005) > 1e-6
