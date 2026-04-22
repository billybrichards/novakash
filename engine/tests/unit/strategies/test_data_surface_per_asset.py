"""Per-asset V4 snapshot cache tests for DataSurfaceManager (audit #267).

Prior behaviour: a single-slot cache always held BTC's payload. ETH/SOL/XRP
windows silently read BTC's v4 snapshot into their surface — probability_up,
probability_classifier, regime, macro, poly block — all wrong asset. This
module pins the per-asset correctness of the cache so the bug cannot recur.
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.data_surface import DataSurfaceManager, FullDataSurface


class _Window:
    def __init__(self, asset: str = "BTC", timeframe: str = "15m"):
        self.asset = asset
        self.timeframe = timeframe
        self.window_ts = 1713010800
        self.open_price = 100.0
        self.eval_offset = 350
        self.up_price = 0.55
        self.down_price = 0.45


def _snapshot(asset: str, *, p_classifier: float, with_poly: bool = False) -> dict:
    """Minimal realistic /v4/snapshot shape for one asset."""
    ts15 = {"probability_classifier": p_classifier}
    if with_poly:
        ts15["polymarket_live_recommended_outcome"] = {
            "direction": "UP" if p_classifier > 0.5 else "DOWN",
            "trade_advised": True,
            "confidence": p_classifier,
            "timing": "optimal",
        }
    return {
        "ts": time.time(),
        "status": "ok" if with_poly else "no_model",
        "asset": asset,
        "timescales": {
            "5m": {
                "polymarket_live_recommended_outcome": {
                    "direction": "UP",
                    "trade_advised": True,
                    "timing": "optimal",
                }
            } if with_poly else {},
            "15m": ts15,
        },
    }


def _mgr(assets=None) -> DataSurfaceManager:
    return DataSurfaceManager(
        v4_base_url="http://fake",
        active_assets=assets or ["BTC", "ETH", "SOL", "XRP"],
    )


def test_active_assets_default_btc_only():
    """Backward compat: default caller (no active_assets) polls BTC only."""
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    assert mgr._active_assets == ["BTC"]


def test_active_assets_setter_uppercases():
    mgr = _mgr(["btc", "eth"])
    assert mgr._active_assets == ["BTC", "ETH"]
    mgr.set_active_assets(["sol", "XRP"])
    assert mgr._active_assets == ["SOL", "XRP"]


def test_cache_slots_are_independent_per_asset():
    """Writing ETH must NOT overwrite BTC's cache slot, and vice versa."""
    mgr = _mgr()
    mgr._cached_v4["BTC"] = _snapshot("BTC", p_classifier=0.70, with_poly=True)
    mgr._cached_v4_ts["BTC"] = time.time()
    mgr._cached_v4["ETH"] = _snapshot("ETH", p_classifier=0.30)
    mgr._cached_v4_ts["ETH"] = time.time()

    btc_surface = mgr.get_surface(_Window(asset="BTC"), 350)
    eth_surface = mgr.get_surface(_Window(asset="ETH"), 350)

    assert btc_surface.probability_classifier == 0.70
    assert eth_surface.probability_classifier == 0.30
    # BTC has poly populated; ETH doesn't.
    assert btc_surface.poly_trade_advised is True
    assert eth_surface.poly_trade_advised is None


def test_missing_asset_returns_empty_surface_not_neighbour_data():
    """Before fix, a XRP window with no XRP cache would read BTC's payload.

    Now: no XRP cache → surface.probability_classifier is None, poly is None.
    """
    mgr = _mgr()
    mgr._cached_v4["BTC"] = _snapshot("BTC", p_classifier=0.70, with_poly=True)
    mgr._cached_v4_ts["BTC"] = time.time()
    # No XRP entry — intentionally.

    surface = mgr.get_surface(_Window(asset="XRP"), 350)

    # Must NOT inherit BTC's 0.70
    assert surface.probability_classifier is None
    assert surface.poly_trade_advised is None
    assert surface.poly_direction is None


def test_four_assets_four_distinct_caches():
    """BTC, ETH, SOL, XRP all present — each returns its own classifier."""
    mgr = _mgr()
    data = {
        "BTC": 0.62,
        "ETH": 0.13,
        "SOL": 0.88,
        "XRP": 0.45,
    }
    for asset, p in data.items():
        mgr._cached_v4[asset] = _snapshot(
            asset, p_classifier=p, with_poly=(asset == "BTC")
        )
        mgr._cached_v4_ts[asset] = time.time()

    for asset, expected in data.items():
        surface = mgr.get_surface(_Window(asset=asset), 350)
        assert surface.probability_classifier == expected, (
            f"{asset} read wrong classifier; got "
            f"{surface.probability_classifier}, expected {expected}"
        )
        assert surface.asset == asset


def test_stale_cache_per_asset_falls_through_to_poly_none():
    """An asset with cache > 60s old must be rejected — for that asset only."""
    mgr = _mgr()
    now = time.time()
    mgr._cached_v4["BTC"] = _snapshot("BTC", p_classifier=0.70, with_poly=True)
    mgr._cached_v4_ts["BTC"] = now  # fresh
    mgr._cached_v4["ETH"] = _snapshot("ETH", p_classifier=0.30)
    mgr._cached_v4_ts["ETH"] = now - 120  # stale, >60s

    btc = mgr.get_surface(_Window(asset="BTC"), 350)
    eth = mgr.get_surface(_Window(asset="ETH"), 350)

    # BTC still has its data
    assert btc.probability_classifier == 0.70
    # ETH's stale cache rejected — surface reads as absent
    assert eth.probability_classifier is None
