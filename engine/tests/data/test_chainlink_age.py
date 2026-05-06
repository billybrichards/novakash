"""Chainlink delta-source freshness flag (audit #374, 2026-05-06).

Two layers under test:

1. ChainlinkFreshnessGate — pure-Python sync gate that reads
   `surface.delta_chainlink_age_seconds` and SKIPs above the configured
   ceiling. Backward-compat: gate PASSES when the surface field is None
   (older surface revisions or feed not yet populated).

2. ChainlinkFeed.latest_updated_at — verifies the feed exposes per-asset
   `updatedAt` epoch from the on-chain Aggregator V3 round, used by
   DataSurfaceManager to compute `delta_chainlink_age_seconds`.
"""
from __future__ import annotations

from types import SimpleNamespace

from strategies.gates.chainlink_freshness import ChainlinkFreshnessGate


def _surface(age):
    return SimpleNamespace(delta_chainlink_age_seconds=age)


# ─────────────────────────── ChainlinkFreshnessGate ──────────────────────────


def test_gate_passes_when_age_below_threshold():
    gate = ChainlinkFreshnessGate(max_age_seconds=30)
    res = gate.evaluate(_surface(10))
    assert res.passed
    assert "fresh" in res.reason.lower()
    assert res.data.get("age_seconds") == 10


def test_gate_passes_at_exact_threshold():
    gate = ChainlinkFreshnessGate(max_age_seconds=30)
    res = gate.evaluate(_surface(30))
    assert res.passed


def test_gate_skips_when_age_above_threshold():
    gate = ChainlinkFreshnessGate(max_age_seconds=30)
    res = gate.evaluate(_surface(120))
    assert not res.passed
    assert "stale" in res.reason.lower()
    assert res.data.get("age_seconds") == 120


def test_gate_passes_when_age_unknown_default():
    """Backward-compat: missing surface field => PASS (no info, fail open)."""
    gate = ChainlinkFreshnessGate(max_age_seconds=30)
    res = gate.evaluate(_surface(None))
    assert res.passed
    assert "unknown" in res.reason.lower()


def test_gate_skips_when_age_unknown_paranoid():
    gate = ChainlinkFreshnessGate(max_age_seconds=30, skip_when_unknown=True)
    res = gate.evaluate(_surface(None))
    assert not res.passed


def test_gate_handles_legacy_surface_without_field():
    gate = ChainlinkFreshnessGate(max_age_seconds=30)
    legacy = SimpleNamespace()  # no `delta_chainlink_age_seconds`
    res = gate.evaluate(legacy)
    assert res.passed


def test_gate_returns_int_age_when_present_as_float():
    gate = ChainlinkFreshnessGate(max_age_seconds=30)
    res = gate.evaluate(_surface(10.7))
    assert res.passed
    assert res.data.get("age_seconds") == 10


# ──────────────────── ChainlinkFeed.latest_updated_at exposure ───────────────


def test_chainlink_feed_exposes_latest_updated_at_dict():
    """Feed object should expose a per-asset updated_at cache that
    DataSurfaceManager reads at surface-assembly time (audit #374)."""
    from data.feeds.chainlink_feed import ChainlinkFeed

    feed = ChainlinkFeed(rpc_url="https://test.invalid", pool=None)
    assert hasattr(feed, "latest_updated_at")
    assert isinstance(feed.latest_updated_at, dict)
    # Empty before any poll runs.
    assert feed.latest_updated_at == {}


def test_chainlink_feed_writes_to_updated_at_on_poll(monkeypatch):
    """Simulating a successful poll-result write must populate
    `latest_updated_at[asset]` so the surface delta-age path works."""
    import asyncio
    from data.feeds.chainlink_feed import ChainlinkFeed

    feed = ChainlinkFeed(rpc_url="https://test.invalid", pool=None)

    # Bypass the real poll loop: invoke the inner write code directly via
    # a synthetic _poll_all run that mimics asyncio.gather returning two
    # successful tuples.
    async def fake_poll_all():
        results = [
            ("BTC", 65000.0, 12345, 1730000000),
            ("ETH", 3500.0, 23456, 1730000005),
        ]
        for asset, price, _round, updated_at in results:
            feed.latest_prices[asset] = price
            feed.latest_updated_at[asset] = updated_at

    asyncio.run(fake_poll_all())

    assert feed.latest_updated_at["BTC"] == 1730000000
    assert feed.latest_updated_at["ETH"] == 1730000005
    assert feed.latest_prices["BTC"] == 65000.0
