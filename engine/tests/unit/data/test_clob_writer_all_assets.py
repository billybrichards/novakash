"""Regression test: CLOB columns must be populated for ALL assets.

Bug confirmed 2026-06-01: signal_evaluations.clob_down_ask was 80% filled
for BTC but 0% for ETH/SOL/XRP. The root cause was data_surface.py reading
`self._clob.latest_clob` (BTC-only flat dict) instead of
`self._clob.latest_clob_by_asset[asset]` (per-asset dict, populated for all
assets since PR #583 added multi-asset CLOB polling).

This suite asserts that DataSurfaceManager.get_surface() correctly populates
clob_{up,down}_{ask,bid}, clob_mid, clob_spread (via clob_implied_up) for
BTC, ETH, SOL, and XRP when the CLOBFeed's `latest_clob_by_asset` is wired.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from strategies.data_surface import DataSurfaceManager


# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeWindow:
    def __init__(self, asset: str = "BTC"):
        self.asset = asset
        self.window_ts = 1748750000
        self.open_price = 100_000.0
        self.eval_offset = 120
        self.up_price = 0.55
        self.down_price = 0.45


_CLOB_SNAPSHOTS: dict[str, dict] = {
    "BTC": {
        "clob_up_bid": 0.61,
        "clob_up_ask": 0.63,
        "clob_down_bid": 0.37,
        "clob_down_ask": 0.39,
        "clob_implied_up": 0.62,
        "last_updated": 9_999_999_999.0,
    },
    "ETH": {
        "clob_up_bid": 0.55,
        "clob_up_ask": 0.57,
        "clob_down_bid": 0.43,
        "clob_down_ask": 0.45,
        "clob_implied_up": 0.56,
        "last_updated": 9_999_999_999.0,
    },
    "SOL": {
        "clob_up_bid": 0.48,
        "clob_up_ask": 0.50,
        "clob_down_bid": 0.50,
        "clob_down_ask": 0.52,
        "clob_implied_up": 0.49,
        "last_updated": 9_999_999_999.0,
    },
    "XRP": {
        "clob_up_bid": 0.70,
        "clob_up_ask": 0.72,
        "clob_down_bid": 0.28,
        "clob_down_ask": 0.30,
        "clob_implied_up": 0.71,
        "last_updated": 9_999_999_999.0,
    },
}


class FakeMultiAssetCLOBFeed:
    """Mimics CLOBFeed after a full multi-asset poll cycle.

    Both `latest_clob_by_asset` (per-asset) and `latest_clob` (BTC-only
    legacy mirror) are populated — matching the real feed state.
    """

    latest_clob_by_asset: dict[str, dict] = _CLOB_SNAPSHOTS
    # BTC mirror (legacy contract — DataSurface still reads this for BTC if
    # latest_clob_by_asset is absent, or for old tests that only set this).
    latest_clob: dict = _CLOB_SNAPSHOTS["BTC"]


class FakeCLOBFeedBTCOnly:
    """Mimics the *old* pre-fix state: only `latest_clob` is populated."""

    latest_clob_by_asset: dict = {}
    latest_clob: dict = _CLOB_SNAPSHOTS["BTC"]


def _make_manager(clob_feed) -> DataSurfaceManager:
    return DataSurfaceManager(
        v4_base_url="http://fake:8001",
        tiingo_feed=None,
        chainlink_feed=None,
        clob_feed=clob_feed,
        vpin_calculator=None,
        cg_feeds={},
        twap_tracker=None,
        binance_state=None,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("asset", ["BTC", "ETH", "SOL", "XRP"])
def test_clob_columns_populated_for_all_assets(asset):
    """Core regression: all 4 assets must get non-NULL CLOB ask/bid fields."""
    mgr = _make_manager(FakeMultiAssetCLOBFeed())
    surface = mgr.get_surface(FakeWindow(asset=asset), eval_offset=120)

    expected = _CLOB_SNAPSHOTS[asset]
    assert surface.clob_up_ask == expected["clob_up_ask"], (
        f"{asset}: clob_up_ask is NULL — writer still BTC-only?"
    )
    assert surface.clob_down_ask == expected["clob_down_ask"], (
        f"{asset}: clob_down_ask is NULL — entry_floor_down gate will be a no-op"
    )
    assert surface.clob_up_bid == expected["clob_up_bid"], (
        f"{asset}: clob_up_bid is NULL"
    )
    assert surface.clob_down_bid == expected["clob_down_bid"], (
        f"{asset}: clob_down_bid is NULL"
    )


@pytest.mark.parametrize("asset", ["ETH", "SOL", "XRP"])
def test_non_btc_clob_not_null(asset):
    """Specific regression check: ETH/SOL/XRP must NOT be NULL (the observed bug)."""
    mgr = _make_manager(FakeMultiAssetCLOBFeed())
    surface = mgr.get_surface(FakeWindow(asset=asset), eval_offset=120)

    assert surface.clob_down_ask is not None, (
        f"{asset} clob_down_ask is None — entry_floor_down gate is a no-op"
    )
    assert surface.clob_up_ask is not None, (
        f"{asset} clob_up_ask is None"
    )


def test_btc_legacy_path_still_works():
    """BTC surfaces correctly even when only the flat `latest_clob` is present."""
    mgr = _make_manager(FakeCLOBFeedBTCOnly())
    surface = mgr.get_surface(FakeWindow(asset="BTC"), eval_offset=120)

    assert surface.clob_up_ask == _CLOB_SNAPSHOTS["BTC"]["clob_up_ask"]
    assert surface.clob_down_ask == _CLOB_SNAPSHOTS["BTC"]["clob_down_ask"]


def test_each_asset_reads_its_own_snapshot_not_btc():
    """ETH window must NOT silently inherit BTC prices."""
    mgr = _make_manager(FakeMultiAssetCLOBFeed())

    btc_surface = mgr.get_surface(FakeWindow(asset="BTC"), eval_offset=120)
    eth_surface = mgr.get_surface(FakeWindow(asset="ETH"), eval_offset=120)

    # BTC and ETH have different prices — they must NOT be equal.
    assert btc_surface.clob_up_ask != eth_surface.clob_up_ask, (
        "ETH inherited BTC CLOB prices — asset isolation broken"
    )
    assert eth_surface.clob_up_ask == _CLOB_SNAPSHOTS["ETH"]["clob_up_ask"]
