"""Classifier shadow-read wiring tests (note #226, phase 1a).

The primary /v4/snapshot host is flipped to the classifier box so the
engine can log ``probability_classifier`` (pc) on every strategy
decision. This module pins the new behaviour:

  1. ``TIMESFM_FALLBACK_URL`` is picked up from the env and used to
     retry against the pre-flip primary when the classifier box fails.
  2. Fetch latency + source are recorded per asset for burn-in p95
     checks (promotion checklist item 6).
  3. Fallback is not attempted when not configured — we keep the
     existing one-shot behaviour in that case.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.data_surface import DataSurfaceManager


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body


class _Session:
    """Minimal aiohttp-like session that routes by base URL prefix.

    ``routes`` maps a base URL (e.g. ``"http://primary"``) to a list of
    ``_Resp`` instances returned in order. Also records every URL hit in
    ``.calls`` so tests can assert fallback ordering.
    """

    def __init__(self, routes: dict[str, list[_Resp]]):
        self._routes = routes
        self.calls: list[str] = []

    def get(self, url: str, params: dict | None = None):
        self.calls.append(url)
        for base, queue in self._routes.items():
            if url.startswith(base):
                if queue:
                    return queue.pop(0)
                raise RuntimeError(f"no more responses for {base}")
        raise RuntimeError(f"no route for {url}")


def _ok_btc_payload() -> dict:
    return {
        "ts": time.time(),
        "status": "ok",
        "asset": "BTC",
        "timescales": {
            "5m": {
                "probability_lgb": 0.71,
                "probability_classifier": 0.62,
                "polymarket_live_recommended_outcome": {
                    "direction": "UP",
                    "trade_advised": True,
                    "timing": "optimal",
                    "confidence": 0.62,
                },
            },
            "15m": {"probability_classifier": 0.55},
        },
    }


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_fallback_url_from_env(monkeypatch):
    """TIMESFM_FALLBACK_URL is picked up at __init__ time."""
    monkeypatch.setenv("TIMESFM_FALLBACK_URL", "http://fallback:8080")
    # Clear other sources of v4_base_url — this test only checks the env var.
    mgr = DataSurfaceManager(v4_base_url="http://primary")
    assert mgr._v4_fallback_url == "http://fallback:8080"


def test_no_fallback_when_env_unset(monkeypatch):
    monkeypatch.delenv("TIMESFM_FALLBACK_URL", raising=False)
    mgr = DataSurfaceManager(v4_base_url="http://primary")
    assert mgr._v4_fallback_url is None


def test_empty_string_fallback_treated_as_none(monkeypatch):
    monkeypatch.setenv("TIMESFM_FALLBACK_URL", "")
    mgr = DataSurfaceManager(v4_base_url="http://primary")
    assert mgr._v4_fallback_url is None


def test_fetch_success_records_latency_and_source():
    """A successful primary fetch stamps pc_fetch_latency_ms + source=primary."""
    mgr = DataSurfaceManager(v4_base_url="http://primary")
    mgr._session = _Session({"http://primary": [_Resp(200, _ok_btc_payload())]})

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mgr._fetch_v4_asset("BTC"))
    finally:
        loop.close()

    tel = mgr.get_fetch_telemetry("BTC")
    assert tel["pc_fetch_source"] == "primary"
    assert "pc_fetch_latency_ms" in tel
    assert tel["pc_fetch_latency_ms"] >= 0.0
    # Cache was populated.
    assert "BTC" in mgr._cached_v4


def test_fetch_falls_back_on_primary_error():
    """Primary non-200 → retry fallback → cache populated with source=fallback."""
    mgr = DataSurfaceManager(
        v4_base_url="http://primary",
        v4_fallback_url="http://fallback",
    )
    mgr._session = _Session({
        "http://primary": [_Resp(503)],
        "http://fallback": [_Resp(200, _ok_btc_payload())],
    })

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mgr._fetch_v4_asset("BTC"))
    finally:
        loop.close()

    assert "BTC" in mgr._cached_v4
    tel = mgr.get_fetch_telemetry("BTC")
    assert tel["pc_fetch_source"] == "fallback"


def test_no_fallback_attempted_when_not_configured():
    """Primary fails + no fallback configured → single call, cache untouched."""
    mgr = DataSurfaceManager(v4_base_url="http://primary")
    session = _Session({"http://primary": [_Resp(503)]})
    mgr._session = session

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mgr._fetch_v4_asset("BTC"))
    finally:
        loop.close()

    # Exactly one call, no fallback.
    assert len(session.calls) == 1
    assert "BTC" not in mgr._cached_v4
    # No telemetry because no successful fetch.
    assert mgr.get_fetch_telemetry("BTC") == {}


def test_both_hosts_fail_leaves_cache_empty():
    """Primary and fallback both fail → no cache, no telemetry."""
    mgr = DataSurfaceManager(
        v4_base_url="http://primary",
        v4_fallback_url="http://fallback",
    )
    session = _Session({
        "http://primary": [_Resp(500)],
        "http://fallback": [_Resp(500)],
    })
    mgr._session = session

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mgr._fetch_v4_asset("BTC"))
    finally:
        loop.close()

    # Two calls (primary + fallback), neither succeeded.
    assert len(session.calls) == 2
    assert "BTC" not in mgr._cached_v4


def test_get_fetch_telemetry_empty_when_no_fetch():
    mgr = DataSurfaceManager(v4_base_url="http://primary")
    assert mgr.get_fetch_telemetry("BTC") == {}
    assert mgr.get_fetch_telemetry("ETH") == {}


def test_telemetry_is_per_asset():
    """BTC and ETH fetches record independent telemetry."""
    mgr = DataSurfaceManager(
        v4_base_url="http://primary",
        v4_fallback_url="http://fallback",
        active_assets=["BTC", "ETH"],
    )
    # BTC succeeds on primary; ETH fails primary, succeeds on fallback.
    eth_payload = {
        "ts": time.time(),
        "status": "no_model",
        "asset": "ETH",
        "timescales": {
            "5m": {"probability_classifier": 0.48},
            "15m": {"probability_classifier": 0.48},
        },
    }
    mgr._session = _Session({
        "http://primary": [_Resp(200, _ok_btc_payload()), _Resp(502)],
        "http://fallback": [_Resp(200, eth_payload)],
    })

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mgr._fetch_v4_asset("BTC"))
        loop.run_until_complete(mgr._fetch_v4_asset("ETH"))
    finally:
        loop.close()

    assert mgr.get_fetch_telemetry("BTC")["pc_fetch_source"] == "primary"
    assert mgr.get_fetch_telemetry("ETH")["pc_fetch_source"] == "fallback"
