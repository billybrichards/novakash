"""Tests for `compute_binance_depth_imbalance` (PR follow-up to #582).

The CLOB + OI emitters already have a sister test file
(test_feature_emitters.py). This file extends coverage for the new
Binance depth helper added in the same PR as the feed.

Coverage:
  - In-memory snapshot fast path (no DB hit).
  - Stale snapshot triggers DB fallback.
  - DB fallback returns the 4 features.
  - All error paths return the all-None dict (never raise).
  - Empty asset string returns all-None.
  - depth_feed=None + pool=None returns all-None.
"""

from __future__ import annotations

import time
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from signals.feature_emitters import (
    _empty_binance_depth,
    compute_binance_depth_from_snapshot,
    compute_binance_depth_imbalance,
)


# ── Pure helper ─────────────────────────────────────────────────────


def test_empty_dict_has_all_4_keys():
    d = _empty_binance_depth()
    assert set(d.keys()) == {
        "binance_depth_imbalance_inner",
        "binance_depth_imbalance_1pct",
        "binance_depth_imbalance_5pct",
        "binance_spread_pct",
    }
    assert all(v is None for v in d.values())


def test_from_snapshot_none_input():
    assert compute_binance_depth_from_snapshot(None) == _empty_binance_depth()


def test_from_snapshot_full_input():
    snap = {
        "best_bid_qty": 5.0,
        "best_ask_qty": 5.0,
        "bid_depth_1pct": 10.0,
        "ask_depth_1pct": 8.0,
        "bid_depth_5pct": 100.0,
        "ask_depth_5pct": 80.0,
        "spread_pct": 0.01,
    }
    d = compute_binance_depth_from_snapshot(snap)
    assert d["binance_depth_imbalance_inner"] == 0.0
    # (10 - 8) / 18 = 0.111…
    assert d["binance_depth_imbalance_1pct"] == pytest.approx(2 / 18)
    # (100 - 80) / 180
    assert d["binance_depth_imbalance_5pct"] == pytest.approx(20 / 180)
    assert d["binance_spread_pct"] == 0.01


def test_from_snapshot_zero_depth_inner_returns_none():
    snap = {
        "best_bid_qty": 0,
        "best_ask_qty": 0,
        "bid_depth_1pct": 1,
        "ask_depth_1pct": 1,
        "bid_depth_5pct": 1,
        "ask_depth_5pct": 1,
        "spread_pct": 0.01,
    }
    d = compute_binance_depth_from_snapshot(snap)
    assert d["binance_depth_imbalance_inner"] is None
    assert d["binance_depth_imbalance_1pct"] == 0.0


def test_from_snapshot_partial_fields_safe():
    # spread_pct missing → None.
    d = compute_binance_depth_from_snapshot({"best_bid_qty": 1, "best_ask_qty": 1})
    assert d["binance_depth_imbalance_inner"] == 0.0
    assert d["binance_spread_pct"] is None


# ── Async compute_binance_depth_imbalance ──────────────────────────


class FakeDepthFeed:
    def __init__(self, snapshots_by_asset: dict[str, Optional[dict]]) -> None:
        self._snaps = snapshots_by_asset

    def latest_snapshot(self, asset: str) -> Optional[dict]:
        return self._snaps.get(asset.upper())


@pytest.mark.asyncio
async def test_returns_all_none_for_empty_asset():
    d = await compute_binance_depth_imbalance(pool=None, asset="")
    assert d == _empty_binance_depth()


@pytest.mark.asyncio
async def test_no_feed_no_pool_returns_all_none():
    d = await compute_binance_depth_imbalance(pool=None, asset="BTC")
    assert d == _empty_binance_depth()


@pytest.mark.asyncio
async def test_in_memory_fast_path():
    """Recent snapshot → derive features without DB hit."""
    fresh_snap = {
        "best_bid_qty": 3.0,
        "best_ask_qty": 7.0,
        "bid_depth_1pct": 1.0,
        "ask_depth_1pct": 1.0,
        "bid_depth_5pct": 1.0,
        "ask_depth_5pct": 1.0,
        "spread_pct": 0.02,
        "ts": time.time(),
    }
    feed = FakeDepthFeed({"BTC": fresh_snap})

    # Pool must NOT be hit — pass None pool with a feed.
    d = await compute_binance_depth_imbalance(
        pool=None, asset="BTC", depth_feed=feed
    )
    # (3 - 7) / 10 = -0.4
    assert d["binance_depth_imbalance_inner"] == pytest.approx(-0.4)
    assert d["binance_spread_pct"] == 0.02


@pytest.mark.asyncio
async def test_stale_snapshot_triggers_db_fallback():
    """ts older than max_age_seconds → DB query."""
    stale_snap = {
        "best_bid_qty": 1.0,
        "best_ask_qty": 1.0,
        "ts": time.time() - 9999,
    }
    feed = FakeDepthFeed({"BTC": stale_snap})

    # DB returns a row with different data.
    pool = MagicMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "best_bid_qty": 9.0,
            "best_ask_qty": 1.0,
            "bid_depth_1pct": 1.0,
            "ask_depth_1pct": 1.0,
            "bid_depth_5pct": 1.0,
            "ask_depth_5pct": 1.0,
            "spread_pct": 0.05,
            "ts": None,
        }
    )

    class FakeAcquire:
        async def __aenter__(self_):
            return conn

        async def __aexit__(self_, *exc):
            return None

    pool.acquire = MagicMock(return_value=FakeAcquire())

    d = await compute_binance_depth_imbalance(
        pool=pool, asset="BTC", depth_feed=feed, max_age_seconds=30
    )
    # Should reflect the DB row, not the stale snapshot.
    # (9 - 1) / 10 = 0.8
    assert d["binance_depth_imbalance_inner"] == pytest.approx(0.8)
    assert d["binance_spread_pct"] == 0.05


@pytest.mark.asyncio
async def test_db_query_no_row_returns_all_none():
    pool = MagicMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    class FakeAcquire:
        async def __aenter__(self_):
            return conn

        async def __aexit__(self_, *exc):
            return None

    pool.acquire = MagicMock(return_value=FakeAcquire())

    d = await compute_binance_depth_imbalance(pool=pool, asset="BTC")
    assert d == _empty_binance_depth()


@pytest.mark.asyncio
async def test_db_query_error_swallowed():
    pool = MagicMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("timeout"))

    class FakeAcquire:
        async def __aenter__(self_):
            return conn

        async def __aexit__(self_, *exc):
            return None

    pool.acquire = MagicMock(return_value=FakeAcquire())

    d = await compute_binance_depth_imbalance(pool=pool, asset="BTC")
    assert d == _empty_binance_depth()


@pytest.mark.asyncio
async def test_feed_raises_falls_back_to_db():
    """If depth_feed.latest_snapshot raises, we don't crash — fall through."""

    class BadFeed:
        def latest_snapshot(self, asset):
            raise RuntimeError("boom")

    pool = MagicMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    class FakeAcquire:
        async def __aenter__(self_):
            return conn

        async def __aexit__(self_, *exc):
            return None

    pool.acquire = MagicMock(return_value=FakeAcquire())

    d = await compute_binance_depth_imbalance(
        pool=pool, asset="BTC", depth_feed=BadFeed()
    )
    assert d == _empty_binance_depth()


@pytest.mark.asyncio
async def test_snapshot_no_ts_treated_as_fresh():
    """If snapshot lacks `ts`, don't reject it — use it as fresh."""
    snap = {
        "best_bid_qty": 1.0,
        "best_ask_qty": 1.0,
        "spread_pct": 0.0,
    }
    feed = FakeDepthFeed({"ETH": snap})

    d = await compute_binance_depth_imbalance(
        pool=None, asset="ETH", depth_feed=feed
    )
    assert d["binance_depth_imbalance_inner"] == 0.0
    assert d["binance_spread_pct"] == 0.0
