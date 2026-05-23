"""Tests for the multi-asset CLOB feed (PR follow-up to #582).

Covers:
  - Per-asset iteration honors `FIVE_MIN_ASSETS` env var.
  - BTC-only legacy `latest_clob` mirror is preserved.
  - `latest_clob_by_asset` is keyed by every asset that polled.
  - Per-asset failures don't abort the loop.
  - Assets without a current window are silently skipped.
  - Writer passes the correct asset to both `ticks_clob` and
    `clob_book_snapshots` (no more `'BTC'` hardcoding).

The polymarket client and DB pool are stubbed end-to-end so this
suite never touches the network or RDS.
"""

from __future__ import annotations

import os
from typing import Optional
from unittest.mock import patch

import pytest

from data.feeds.clob_feed import CLOBFeed, _resolve_assets


# ── Fakes ────────────────────────────────────────────────────────────


class FakeWindow:
    def __init__(
        self,
        asset: str,
        window_ts: int = 1778959500,
        up_token_id: str = "up-tok",
        down_token_id: str = "down-tok",
        duration_secs: int = 300,
    ) -> None:
        self.asset = asset
        self.window_ts = window_ts
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self.duration_secs = duration_secs


class FakePolymarketFeed:
    """Stub of Polymarket5MinFeed.get_current_window(asset)."""

    def __init__(self, windows_by_asset: dict[str, Optional[FakeWindow]]) -> None:
        self._windows = windows_by_asset

    def get_current_window(self, asset: str) -> Optional[FakeWindow]:
        return self._windows.get(asset)


class FakePolyClient:
    """Stub of PolymarketClient.get_clob_order_book()."""

    def __init__(
        self,
        books_by_token: Optional[dict[str, dict]] = None,
        raise_for_token: Optional[set[str]] = None,
    ) -> None:
        self._books = books_by_token or {}
        self._raise = raise_for_token or set()
        self.calls: list[str] = []

    async def get_clob_order_book(self, token_id: str) -> dict:
        self.calls.append(token_id)
        if token_id in self._raise:
            raise RuntimeError(f"polymarket 503 for {token_id}")
        return self._books.get(token_id, {"best_bid": 0.5, "best_ask": 0.52})


class FakeConn:
    def __init__(self, parent: "FakePool") -> None:
        self._parent = parent

    async def execute(self, sql: str, *args) -> None:
        # Record (sql_first_token, args) so tests can assert table + asset.
        self._parent.executions.append((sql.strip()[:60], args))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


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


# ── _resolve_assets ──────────────────────────────────────────────────


def test_resolve_assets_default_btc(monkeypatch):
    monkeypatch.delenv("FIVE_MIN_ASSETS", raising=False)
    assert _resolve_assets() == ["BTC"]


def test_resolve_assets_csv_parsed(monkeypatch):
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC,ETH,XRP")
    assert _resolve_assets() == ["BTC", "ETH", "XRP"]


def test_resolve_assets_whitespace_and_case(monkeypatch):
    monkeypatch.setenv("FIVE_MIN_ASSETS", " btc , Eth , xrp ")
    assert _resolve_assets() == ["BTC", "ETH", "XRP"]


def test_resolve_assets_empty_falls_back(monkeypatch):
    monkeypatch.setenv("FIVE_MIN_ASSETS", "")
    assert _resolve_assets() == ["BTC"]


def test_resolve_assets_ignores_blank_tokens(monkeypatch):
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC,,ETH,")
    assert _resolve_assets() == ["BTC", "ETH"]


# ── _poll: per-asset iteration ───────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_iterates_all_enabled_assets(monkeypatch):
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC,ETH,XRP")
    windows = {
        "BTC": FakeWindow("BTC", up_token_id="btc-up", down_token_id="btc-dn"),
        "ETH": FakeWindow("ETH", up_token_id="eth-up", down_token_id="eth-dn"),
        "XRP": FakeWindow("XRP", up_token_id="xrp-up", down_token_id="xrp-dn"),
    }
    poly = FakePolyClient()
    pool = FakePool()
    feed = CLOBFeed(
        poly_client=poly,
        db_pool=pool,
        polymarket_feed=FakePolymarketFeed(windows),
    )

    await feed._poll()

    # Three assets × 2 token fetches each = 6 polymarket calls.
    assert len(poly.calls) == 6
    assert "btc-up" in poly.calls and "btc-dn" in poly.calls
    assert "eth-up" in poly.calls and "eth-dn" in poly.calls
    assert "xrp-up" in poly.calls and "xrp-dn" in poly.calls


@pytest.mark.asyncio
async def test_latest_clob_by_asset_keyed_per_asset(monkeypatch):
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC,ETH")
    windows = {
        "BTC": FakeWindow("BTC", up_token_id="btc-up", down_token_id="btc-dn"),
        "ETH": FakeWindow("ETH", up_token_id="eth-up", down_token_id="eth-dn"),
    }
    poly = FakePolyClient(
        books_by_token={
            "btc-up": {"best_bid": 0.61, "best_ask": 0.63},
            "btc-dn": {"best_bid": 0.37, "best_ask": 0.39},
            "eth-up": {"best_bid": 0.55, "best_ask": 0.57},
            "eth-dn": {"best_bid": 0.43, "best_ask": 0.45},
        }
    )
    feed = CLOBFeed(
        poly_client=poly,
        db_pool=FakePool(),
        polymarket_feed=FakePolymarketFeed(windows),
    )

    await feed._poll()

    assert "BTC" in feed.latest_clob_by_asset
    assert "ETH" in feed.latest_clob_by_asset
    assert feed.latest_clob_by_asset["BTC"]["clob_up_bid"] == 0.61
    assert feed.latest_clob_by_asset["ETH"]["clob_up_bid"] == 0.55


@pytest.mark.asyncio
async def test_btc_mirrors_into_legacy_latest_clob(monkeypatch):
    """Backwards-compat: `latest_clob` flat dict still tracks BTC."""
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC,ETH")
    windows = {
        "BTC": FakeWindow("BTC", up_token_id="btc-up", down_token_id="btc-dn"),
        "ETH": FakeWindow("ETH", up_token_id="eth-up", down_token_id="eth-dn"),
    }
    poly = FakePolyClient(
        books_by_token={
            "btc-up": {"best_bid": 0.61, "best_ask": 0.63},
            "btc-dn": {"best_bid": 0.37, "best_ask": 0.39},
            "eth-up": {"best_bid": 0.99, "best_ask": 0.99},
            "eth-dn": {"best_bid": 0.99, "best_ask": 0.99},
        }
    )
    feed = CLOBFeed(
        poly_client=poly,
        db_pool=FakePool(),
        polymarket_feed=FakePolymarketFeed(windows),
    )

    await feed._poll()

    # latest_clob carries BTC values, NOT ETH (regardless of iteration order).
    assert feed.latest_clob["clob_up_bid"] == 0.61
    assert feed.latest_clob["clob_up_ask"] == 0.63


@pytest.mark.asyncio
async def test_eth_only_does_not_populate_legacy_latest_clob(monkeypatch):
    """If BTC is NOT in FIVE_MIN_ASSETS, legacy mirror stays empty."""
    monkeypatch.setenv("FIVE_MIN_ASSETS", "ETH")
    windows = {"ETH": FakeWindow("ETH")}
    feed = CLOBFeed(
        poly_client=FakePolyClient(),
        db_pool=FakePool(),
        polymarket_feed=FakePolymarketFeed(windows),
    )

    await feed._poll()

    # ETH snapshot recorded by_asset; BTC mirror unchanged.
    assert "ETH" in feed.latest_clob_by_asset
    assert feed.latest_clob == {}


@pytest.mark.asyncio
async def test_missing_window_for_asset_skips_silently(monkeypatch):
    """If Polymarket has no market for an asset, that asset is skipped."""
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC,ETH,XRP")
    windows = {
        "BTC": FakeWindow("BTC"),
        # ETH has no current market
        "ETH": None,
        # XRP window has no token_ids yet
        "XRP": FakeWindow("XRP", up_token_id=None, down_token_id=None),
    }
    poly = FakePolyClient()
    feed = CLOBFeed(
        poly_client=poly,
        db_pool=FakePool(),
        polymarket_feed=FakePolymarketFeed(windows),
    )

    await feed._poll()

    # Only BTC polled.
    assert len(poly.calls) == 2
    assert "BTC" in feed.latest_clob_by_asset
    assert "ETH" not in feed.latest_clob_by_asset
    assert "XRP" not in feed.latest_clob_by_asset


@pytest.mark.asyncio
async def test_per_asset_failure_does_not_abort_loop(monkeypatch):
    """ETH polymarket error must NOT stop BTC/XRP from polling."""
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC,ETH,XRP")
    windows = {
        "BTC": FakeWindow("BTC", up_token_id="btc-up", down_token_id="btc-dn"),
        "ETH": FakeWindow("ETH", up_token_id="eth-up", down_token_id="eth-dn"),
        "XRP": FakeWindow("XRP", up_token_id="xrp-up", down_token_id="xrp-dn"),
    }
    poly = FakePolyClient(raise_for_token={"eth-up"})
    feed = CLOBFeed(
        poly_client=poly,
        db_pool=FakePool(),
        polymarket_feed=FakePolymarketFeed(windows),
    )

    await feed._poll()

    # BTC + XRP both succeeded; ETH failed and was skipped.
    assert "BTC" in feed.latest_clob_by_asset
    assert "XRP" in feed.latest_clob_by_asset
    assert "ETH" not in feed.latest_clob_by_asset


@pytest.mark.asyncio
async def test_writer_passes_asset_not_btc_hardcoded(monkeypatch):
    """Both ticks_clob and clob_book_snapshots INSERTs must use the correct asset."""
    monkeypatch.setenv("FIVE_MIN_ASSETS", "ETH")
    windows = {"ETH": FakeWindow("ETH", up_token_id="eth-up", down_token_id="eth-dn")}
    pool = FakePool()
    feed = CLOBFeed(
        poly_client=FakePolyClient(),
        db_pool=pool,
        polymarket_feed=FakePolymarketFeed(windows),
    )

    await feed._poll()

    # Two INSERTs: ticks_clob and clob_book_snapshots. Both must pass "ETH".
    assert len(pool.executions) == 2
    for sql_head, args in pool.executions:
        # First positional arg after NOW() is the asset string.
        assert args[0] == "ETH", f"expected ETH, got {args[0]!r} in {sql_head}"


@pytest.mark.asyncio
async def test_writer_per_asset_writes(monkeypatch):
    """BTC + ETH both poll -> 4 INSERTs total (2 tables × 2 assets)."""
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC,ETH")
    windows = {
        "BTC": FakeWindow("BTC", up_token_id="btc-up", down_token_id="btc-dn"),
        "ETH": FakeWindow("ETH", up_token_id="eth-up", down_token_id="eth-dn"),
    }
    pool = FakePool()
    feed = CLOBFeed(
        poly_client=FakePolyClient(),
        db_pool=pool,
        polymarket_feed=FakePolymarketFeed(windows),
    )

    await feed._poll()

    assert len(pool.executions) == 4
    assets_seen = [args[0] for _, args in pool.executions]
    assert sorted(assets_seen) == ["BTC", "BTC", "ETH", "ETH"]


@pytest.mark.asyncio
async def test_no_pool_safe(monkeypatch):
    """Feed without a DB pool must still update in-memory caches."""
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC")
    windows = {"BTC": FakeWindow("BTC")}
    feed = CLOBFeed(
        poly_client=FakePolyClient(),
        db_pool=None,
        polymarket_feed=FakePolymarketFeed(windows),
    )

    await feed._poll()

    assert "BTC" in feed.latest_clob_by_asset


@pytest.mark.asyncio
async def test_no_feed_returns_early():
    """No 5m feed wired -> _poll is a no-op."""
    feed = CLOBFeed(
        poly_client=FakePolyClient(),
        db_pool=FakePool(),
        polymarket_feed=None,
    )
    await feed._poll()  # should not raise
    assert feed.latest_clob_by_asset == {}
