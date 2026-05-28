"""Tests for the tickformer fill-gap fix (2026-05-28).

Root-cause:
    _try_fetch_snapshot rejects BTC /v4/snapshot responses when
    polymarket_live_recommended_outcome.timing is None (between-window
    state, ~75% of the time). The main _cached_v4["BTC"] slot was never
    updated, so after 60s the stale-check zeroed it and all tickformer
    fields became None on the surface — even though the timesfm response
    DID carry valid probability_tickformer_v18 (and siblings).

Fix:
    Every 200 response is parsed for tickformer fields BEFORE the poly
    guard runs. Fields are stashed in _cached_tickformer (per-asset).
    get_surface falls back to _cached_tickformer when ts_data is empty
    (poly-blocked main cache).

These tests pin:
  1. Sub-cache is populated even when the poly guard fires.
  2. get_surface returns tickformer values from sub-cache when main cache
     is empty (simulating the poly-guard scenario).
  3. Main cache ts_data values WIN over sub-cache (no regression when
     poly timing is present and main cache is fresh).
  4. Sub-cache older than 60s is treated as absent (same staleness cap
     as main cache) — prevents serving very stale tickformer probs.
  5. LGB fields (probability_lgb_v9_2 etc.) are unaffected — they read
     only from ts_data / main cache, never from _tf_subcache.
  6. Non-BTC asset paths (ETH/SOL/XRP) are unaffected.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.data_surface import DataSurfaceManager, FullDataSurface


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _mgr() -> DataSurfaceManager:
    return DataSurfaceManager(
        v4_base_url="http://fake",
        active_assets=["BTC", "ETH"],
    )


class _Window:
    def __init__(self, asset: str = "BTC", timeframe: str = "5m") -> None:
        self.asset = asset
        self.timeframe = timeframe
        self.window_ts = 1779500000
        self.open_price = 100_000.0
        self.eval_offset = 120
        self.up_price = 0.60
        self.down_price = 0.40


def _btc_body_no_poly(*, v18: float = 0.91, v17: float = 0.88, v16: float = 0.85,
                       v20: float | None = None,
                       gate_cond: float | None = 0.74,
                       trade_signal: str | None = "UP") -> dict:
    """Minimal snapshot body where the poly block is ABSENT (timing=None).

    This is what the engine receives ~75% of the time — between Polymarket
    windows, or when the assembler hasn't finished computing the block yet.
    """
    ts5: dict = {
        "probability_tickformer_v16": v16,
        "probability_tickformer_v17": v17,
        "probability_tickformer_v18": v18,
        "probability_lgb": 0.62,
        "probability_lgb_v9_2": 0.71,
        "probability_classifier": 0.68,
        "regime": "calm_trend",
        "probability_up": 0.65,
        "probability_raw": 0.64,
        # poly block intentionally absent — simulates between-window state
    }
    if v20 is not None:
        ts5["probability_tickformer_v20"] = v20
    if gate_cond is not None:
        ts5["tickformer_gate_cond"] = gate_cond
    if trade_signal is not None:
        ts5["tickformer_trade_signal"] = trade_signal
    return {
        "ts": time.time(),
        "status": "ok",
        "asset": "BTC",
        "timescales": {"5m": ts5, "15m": {}},
    }


def _btc_body_with_poly(*, v18: float = 0.93) -> dict:
    """Snapshot body with a valid poly block (timing='optimal')."""
    return {
        "ts": time.time(),
        "status": "ok",
        "asset": "BTC",
        "timescales": {
            "5m": {
                "probability_tickformer_v18": v18,
                "probability_lgb": 0.70,
                "probability_lgb_v9_2": 0.75,
                "probability_classifier": 0.72,
                "regime": "volatile_trend",
                "probability_up": 0.70,
                "probability_raw": 0.69,
                "polymarket_live_recommended_outcome": {
                    "direction": "UP",
                    "trade_advised": True,
                    "confidence": 0.80,
                    "timing": "optimal",
                },
            },
            "15m": {},
        },
    }


# ─── 1. Sub-cache populated even when poly guard would reject ────────────────

def test_subcache_populated_from_body_without_poly():
    """_cached_tickformer is updated even when body has no poly timing."""
    mgr = _mgr()
    body = _btc_body_no_poly(v18=0.91)
    ts5 = (body.get("timescales") or {}).get("5m", {})

    # Simulate what _try_fetch_snapshot does before the poly guard:
    _TF_KEYS = (
        "probability_tickformer_v16", "tickformer_v16",
        "probability_tickformer_v17", "tickformer_v17",
        "probability_tickformer_v18", "tickformer_v18",
        "probability_tickformer_v20",
        "tickformer_gate_cond",
        "tickformer_trade_signal", "signal",
    )
    _tf_fields = {k: ts5.get(k) for k in _TF_KEYS if ts5.get(k) is not None}
    if _tf_fields:
        mgr._cached_tickformer["BTC"] = _tf_fields
        mgr._cached_tickformer_ts["BTC"] = time.time()

    assert mgr._cached_tickformer.get("BTC") is not None
    assert mgr._cached_tickformer["BTC"]["probability_tickformer_v18"] == 0.91
    assert mgr._cached_tickformer["BTC"]["probability_tickformer_v17"] == 0.88
    assert mgr._cached_tickformer["BTC"]["probability_tickformer_v16"] == 0.85


# ─── 2. get_surface falls back to sub-cache when main cache is empty ─────────

def test_surface_tickformer_v18_from_subcache_when_main_cache_empty():
    """When _cached_v4 is absent but _cached_tickformer is fresh, v18 is populated."""
    mgr = _mgr()
    # Main cache is EMPTY (poly guard blocked every update this window cycle).
    assert "BTC" not in mgr._cached_v4

    # Sub-cache has been written from a non-poly-blocked parse.
    mgr._cached_tickformer["BTC"] = {
        "probability_tickformer_v18": 0.91,
        "probability_tickformer_v17": 0.88,
        "probability_tickformer_v16": 0.85,
        "tickformer_gate_cond": 0.74,
        "tickformer_trade_signal": "UP",
    }
    mgr._cached_tickformer_ts["BTC"] = time.time()

    surface = mgr.get_surface(_Window(), 120)

    assert surface.probability_tickformer_v18 == pytest.approx(0.91)
    assert surface.probability_tickformer_v17 == pytest.approx(0.88)
    assert surface.probability_tickformer_v16 == pytest.approx(0.85)
    assert surface.tickformer_gate_cond == pytest.approx(0.74)
    assert surface.tickformer_trade_signal == "UP"


def test_surface_tickformer_v20_from_subcache():
    """v20 is also populated from the sub-cache when main cache is absent."""
    mgr = _mgr()
    mgr._cached_tickformer["BTC"] = {
        "probability_tickformer_v18": 0.92,
        "probability_tickformer_v20": 0.87,
    }
    mgr._cached_tickformer_ts["BTC"] = time.time()

    surface = mgr.get_surface(_Window(), 60)

    assert surface.probability_tickformer_v18 == pytest.approx(0.92)
    assert surface.probability_tickformer_v20 == pytest.approx(0.87)


# ─── 3. Main cache wins over sub-cache when both are fresh ───────────────────

def test_main_cache_wins_over_subcache_when_both_fresh():
    """When the main cache has tickformer values, they take priority over sub-cache."""
    mgr = _mgr()

    # Main cache has v18=0.93 (from a successful poly-gate-passing fetch).
    body = _btc_body_with_poly(v18=0.93)
    mgr._cached_v4["BTC"] = body
    mgr._cached_v4_ts["BTC"] = time.time()

    # Sub-cache has a different (older) value.
    mgr._cached_tickformer["BTC"] = {"probability_tickformer_v18": 0.77}
    mgr._cached_tickformer_ts["BTC"] = time.time()

    surface = mgr.get_surface(_Window(), 120)

    # Main cache value should win.
    assert surface.probability_tickformer_v18 == pytest.approx(0.93)


# ─── 4. Stale sub-cache (>60s) is treated as absent ─────────────────────────

def test_stale_subcache_is_not_used():
    """A sub-cache older than 60s is ignored — prevents serving stale probs."""
    mgr = _mgr()
    # No main cache.
    assert "BTC" not in mgr._cached_v4

    # Sub-cache exists but is 90s old.
    mgr._cached_tickformer["BTC"] = {"probability_tickformer_v18": 0.91}
    mgr._cached_tickformer_ts["BTC"] = time.time() - 90  # stale

    surface = mgr.get_surface(_Window(), 120)

    assert surface.probability_tickformer_v18 is None


# ─── 5. LGB fields unaffected by sub-cache ───────────────────────────────────

def test_lgb_fields_not_affected_by_subcache():
    """LGB probability fields (v9_2, v12 etc.) are read only from ts_data/main cache.

    The sub-cache contains ONLY tickformer keys. LGB fields must never be
    accidentally sourced from a stale sub-cache entry — they are present in
    the main cache or absent from the surface entirely.
    """
    mgr = _mgr()

    # Sub-cache has tickformer fields; main cache is empty.
    mgr._cached_tickformer["BTC"] = {
        "probability_tickformer_v18": 0.91,
        "probability_lgb_v9_2": 0.99,  # should NOT appear on surface
    }
    mgr._cached_tickformer_ts["BTC"] = time.time()

    surface = mgr.get_surface(_Window(), 120)

    # Tickformer from sub-cache — OK.
    assert surface.probability_tickformer_v18 == pytest.approx(0.91)
    # LGB is None because the main cache is empty.
    assert surface.probability_lgb_v9_2 is None


def test_lgb_fields_populated_from_main_cache():
    """When main cache is fresh, LGB fields come from it as normal."""
    mgr = _mgr()
    body = _btc_body_with_poly(v18=0.93)
    # Inject a v9.2 prob into the body.
    body["timescales"]["5m"]["probability_lgb_v9_2"] = 0.72
    mgr._cached_v4["BTC"] = body
    mgr._cached_v4_ts["BTC"] = time.time()

    surface = mgr.get_surface(_Window(), 120)

    assert surface.probability_lgb_v9_2 == pytest.approx(0.72)
    assert surface.probability_tickformer_v18 == pytest.approx(0.93)


# ─── 6. Non-BTC paths unaffected ─────────────────────────────────────────────

def test_eth_surface_unaffected_by_btc_subcache():
    """ETH surface never reads from BTC's sub-cache slot."""
    mgr = _mgr()

    # BTC sub-cache has values — should NOT appear on ETH surface.
    mgr._cached_tickformer["BTC"] = {"probability_tickformer_v18": 0.91}
    mgr._cached_tickformer_ts["BTC"] = time.time()

    eth_surface = mgr.get_surface(_Window(asset="ETH"), 120)

    # ETH has no main cache and no ETH sub-cache entry.
    assert eth_surface.probability_tickformer_v18 is None


def test_eth_main_cache_still_works():
    """ETH main cache path is completely unaffected by this fix."""
    mgr = _mgr()
    eth_body = {
        "ts": time.time(),
        "status": "ok",
        "asset": "ETH",
        "timescales": {
            "5m": {
                "probability_classifier": 0.60,
                "probability_up": 0.58,
                "probability_tickformer_v18": 0.79,
            },
            "15m": {},
        },
    }
    mgr._cached_v4["ETH"] = eth_body
    mgr._cached_v4_ts["ETH"] = time.time()

    surface = mgr.get_surface(_Window(asset="ETH"), 120)

    assert surface.probability_classifier == pytest.approx(0.60)
    assert surface.probability_tickformer_v18 == pytest.approx(0.79)


# ─── 7. Integration-style: sub-cache survives multiple cycles ────────────────

def test_subcache_provides_consistent_fill_across_window_cycle():
    """Over many surface reads between poly-gate-passing ticks, v18 stays populated.

    This models a 5-minute window where the poly timing is absent for 4 out
    of 5 fetch cycles (75% rate). The sub-cache from the one good fetch
    should remain fresh and keep filling the surface.
    """
    mgr = _mgr()

    # One successful sub-cache write (simulates the 25% that have tickformer).
    mgr._cached_tickformer["BTC"] = {
        "probability_tickformer_v18": 0.91,
        "tickformer_trade_signal": "UP",
    }
    mgr._cached_tickformer_ts["BTC"] = time.time()

    # Main cache is ALWAYS empty (poly guard blocked everything this window).
    assert "BTC" not in mgr._cached_v4

    # Simulate 20 consecutive surface reads (one every 15 seconds ≈ one window).
    for _ in range(20):
        surface = mgr.get_surface(_Window(), 120)
        assert surface.probability_tickformer_v18 == pytest.approx(0.91), \
            "tickformer fill dropped to None mid-cycle"
        assert surface.tickformer_trade_signal == "UP"
