"""Tests for the Binance depth feed (PR follow-up to #582, gap 3).

Covers:
  - URL generation per asset / venue
  - `parse_depth_message` for futures (e=depthUpdate) + spot (lastUpdateId)
  - Imbalance + spread derivation arithmetic
  - Throttled writer flushes the LATEST snapshot only
  - Multi-asset orchestrator owns one feed per asset
  - Asset → symbol mapping via map + env override
  - Pool=None safe path
  - Stale / crossed / zero books are dropped

No network IO — websockets are mocked.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional
from unittest.mock import patch

import pytest

from data.feeds.binance_depth import (
    BinanceDepthFeed,
    BinanceDepthMultiFeed,
    DEFAULT_BINANCE_SYMBOL_MAP,
    _asset_to_symbol,
    _resolve_assets,
    compute_inner_imbalance,
    compute_pct_imbalance,
    parse_depth_message,
)


# ── _asset_to_symbol ────────────────────────────────────────────────


def test_asset_to_symbol_default_map():
    assert _asset_to_symbol("BTC") == "btcusdt"
    assert _asset_to_symbol("ETH") == "ethusdt"
    assert _asset_to_symbol("XRP") == "xrpusdt"


def test_asset_to_symbol_case_insensitive():
    assert _asset_to_symbol("eth") == "ethusdt"


def test_asset_to_symbol_env_override(monkeypatch):
    monkeypatch.setenv("BINANCE_SYMBOL_BTC", "BTCBUSD")
    assert _asset_to_symbol("BTC") == "btcbusd"


def test_asset_to_symbol_unknown_guesses_usdt():
    assert _asset_to_symbol("FOO") == "foousdt"


# ── _resolve_assets ─────────────────────────────────────────────────


def test_resolve_assets_default(monkeypatch):
    monkeypatch.delenv("FIVE_MIN_ASSETS", raising=False)
    assert _resolve_assets() == ["BTC"]


def test_resolve_assets_csv(monkeypatch):
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC,ETH,XRP")
    assert _resolve_assets() == ["BTC", "ETH", "XRP"]


# ── parse_depth_message ─────────────────────────────────────────────


def test_parse_depth_futures_message():
    payload = {
        "e": "depthUpdate",
        "s": "BTCUSDT",
        "u": 12345,
        "b": [["67890.10", "1.000"], ["67890.00", "2.000"]],
        "a": [["67890.20", "1.500"], ["67890.30", "3.500"]],
    }
    snap = parse_depth_message(payload)
    assert snap is not None
    assert snap["best_bid"] == 67890.10
    assert snap["best_ask"] == 67890.20
    assert snap["best_bid_qty"] == 1.000
    assert snap["best_ask_qty"] == 1.500
    assert snap["mid"] == pytest.approx(67890.15)
    # spread_pct = (0.10 / 67890.15) * 100 ≈ 0.000147
    assert snap["spread_pct"] == pytest.approx((0.10 / 67890.15) * 100, rel=1e-6)
    assert snap["last_update_id"] == 12345


def test_parse_depth_spot_message():
    payload = {
        "lastUpdateId": 999,
        "bids": [["100.0", "1.0"]],
        "asks": [["100.1", "2.0"]],
    }
    snap = parse_depth_message(payload)
    assert snap is not None
    assert snap["best_bid"] == 100.0
    assert snap["best_ask"] == 100.1


def test_parse_depth_empty_payload_returns_none():
    assert parse_depth_message({}) is None
    assert parse_depth_message(None) is None
    assert parse_depth_message({"b": []}) is None


def test_parse_depth_crossed_book_dropped():
    """Best bid > best ask → drop (likely race condition)."""
    payload = {
        "b": [["100.0", "1.0"]],
        "a": [["99.0", "1.0"]],
    }
    assert parse_depth_message(payload) is None


def test_parse_depth_zero_prices_dropped():
    payload = {
        "b": [["0", "1.0"]],
        "a": [["100", "1.0"]],
    }
    assert parse_depth_message(payload) is None


def test_parse_depth_1pct_depth_includes_only_within_band():
    """Bids/asks outside ±1% of mid must be excluded from 1pct depth."""
    # mid ≈ 100. 1% band = [99, 101].
    payload = {
        "b": [
            ["99.5", "10.0"],   # within ±1%
            ["98.0", "100.0"],  # outside ±1% (below)
        ],
        "a": [
            ["100.5", "5.0"],   # within ±1%
            ["102.0", "200.0"], # outside ±1% (above)
        ],
    }
    snap = parse_depth_message(payload)
    assert snap is not None
    # bid_depth_1pct should be 10.0 only (98.0 < lo_1pct=99).
    assert snap["bid_depth_1pct"] == pytest.approx(10.0)
    # ask_depth_1pct should be 5.0 only (102.0 > hi_1pct=101).
    assert snap["ask_depth_1pct"] == pytest.approx(5.0)
    # 5pct band catches ALL of them (95 → 105).
    assert snap["bid_depth_5pct"] == pytest.approx(110.0)
    assert snap["ask_depth_5pct"] == pytest.approx(205.0)


def test_parse_depth_malformed_levels_dropped():
    payload = {
        "b": [["abc", "1.0"]],
        "a": [["100", "1.0"]],
    }
    assert parse_depth_message(payload) is None


# ── Imbalance helpers ───────────────────────────────────────────────


def test_inner_imbalance_balanced():
    assert compute_inner_imbalance(1.0, 1.0) == 0.0


def test_inner_imbalance_bid_heavy():
    assert compute_inner_imbalance(3.0, 1.0) == 0.5


def test_inner_imbalance_ask_heavy():
    assert compute_inner_imbalance(1.0, 3.0) == -0.5


def test_inner_imbalance_none_returns_none():
    assert compute_inner_imbalance(None, 1.0) is None
    assert compute_inner_imbalance(1.0, None) is None


def test_inner_imbalance_zero_sum_returns_none():
    assert compute_inner_imbalance(0.0, 0.0) is None


def test_pct_imbalance_mirrors_inner():
    # Same function, named for clarity.
    assert compute_pct_imbalance(5.0, 3.0) == compute_inner_imbalance(5.0, 3.0)


# ── BinanceDepthFeed: URL + lifecycle ───────────────────────────────


def test_feed_stream_url_futures():
    feed = BinanceDepthFeed(asset="BTC")
    assert feed._stream_url == "wss://fstream.binance.com/ws/btcusdt@depth20@100ms"


def test_feed_stream_url_spot():
    feed = BinanceDepthFeed(asset="ETH", venue="spot")
    assert feed._stream_url == "wss://stream.binance.com:9443/ws/ethusdt@depth20@100ms"


def test_feed_initial_state():
    feed = BinanceDepthFeed(asset="BTC")
    assert feed.connected is False
    assert feed.latest_snapshot is None
    assert feed.last_message_at is None


# ── Writer throttling ───────────────────────────────────────────────


class FakeConn:
    def __init__(self, parent: "FakePool") -> None:
        self._parent = parent

    async def execute(self, sql: str, *args) -> None:
        self._parent.executions.append(args)


class FakePoolAcquire:
    def __init__(self, parent: "FakePool") -> None:
        self._parent = parent

    async def __aenter__(self) -> FakeConn:
        return FakeConn(self._parent)

    async def __aexit__(self, *exc) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.executions: list[tuple] = []

    def acquire(self) -> FakePoolAcquire:
        return FakePoolAcquire(self)


@pytest.mark.asyncio
async def test_flush_snapshot_writes_latest_only():
    """Three back-to-back snapshot updates with one flush in between
    must write only the LATEST snapshot (throttling)."""
    pool = FakePool()
    feed = BinanceDepthFeed(asset="ETH", db_pool=pool)

    # Simulate the reader receiving three messages.
    feed._latest_snapshot = parse_depth_message(
        {"b": [["100.0", "1.0"]], "a": [["100.1", "1.0"]]}
    )
    feed._latest_snapshot = parse_depth_message(
        {"b": [["100.0", "2.0"]], "a": [["100.1", "2.0"]]}
    )
    feed._latest_snapshot = parse_depth_message(
        {"b": [["100.0", "5.0"]], "a": [["100.1", "5.0"]]}
    )

    await feed._flush_snapshot()

    # One INSERT written, with the latest qty=5.0.
    assert len(pool.executions) == 1
    # args order matches the SQL: asset, last_update_id, best_bid, best_ask,
    # best_bid_qty, best_ask_qty, ...
    args = pool.executions[0]
    assert args[0] == "ETH"
    # best_bid_qty is at index 4 (asset, last_update_id, best_bid, best_ask, best_bid_qty)
    assert args[4] == 5.0


@pytest.mark.asyncio
async def test_flush_snapshot_no_snapshot_is_noop():
    pool = FakePool()
    feed = BinanceDepthFeed(asset="BTC", db_pool=pool)
    await feed._flush_snapshot()
    assert pool.executions == []


@pytest.mark.asyncio
async def test_flush_snapshot_no_pool_is_safe():
    feed = BinanceDepthFeed(asset="BTC", db_pool=None)
    feed._latest_snapshot = parse_depth_message(
        {"b": [["100.0", "1.0"]], "a": [["100.1", "1.0"]]}
    )
    # Must not raise.
    await feed._flush_snapshot()


@pytest.mark.asyncio
async def test_flush_swallows_db_errors():
    """DB write errors must be logged + swallowed; never raise into caller."""

    class BadPool:
        def acquire(self):
            class BadAcquire:
                async def __aenter__(self):
                    raise RuntimeError("connection lost")

                async def __aexit__(self, *exc):
                    return None

            return BadAcquire()

    feed = BinanceDepthFeed(asset="BTC", db_pool=BadPool())
    feed._latest_snapshot = parse_depth_message(
        {"b": [["100.0", "1.0"]], "a": [["100.1", "1.0"]]}
    )
    # Must not raise.
    await feed._flush_snapshot()


# ── Multi-asset orchestrator ─────────────────────────────────────────


def test_multi_feed_one_per_asset(monkeypatch):
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC,ETH,XRP")
    multi = BinanceDepthMultiFeed(db_pool=None)
    assert sorted(multi.assets) == ["BTC", "ETH", "XRP"]
    assert "BTC" in multi._feeds
    assert "ETH" in multi._feeds
    assert "XRP" in multi._feeds


def test_multi_feed_explicit_assets():
    multi = BinanceDepthMultiFeed(db_pool=None, assets=["BTC", "SOL"])
    assert sorted(multi.assets) == ["BTC", "SOL"]


def test_multi_feed_latest_snapshot_returns_per_asset():
    multi = BinanceDepthMultiFeed(db_pool=None, assets=["BTC", "ETH"])
    multi._feeds["BTC"]._latest_snapshot = {"best_bid": 67000}
    multi._feeds["ETH"]._latest_snapshot = {"best_bid": 3500}

    assert multi.latest_snapshot("BTC")["best_bid"] == 67000
    assert multi.latest_snapshot("ETH")["best_bid"] == 3500
    # Case-insensitive lookup.
    assert multi.latest_snapshot("btc")["best_bid"] == 67000
    # Unknown asset returns None.
    assert multi.latest_snapshot("DOGE") is None
