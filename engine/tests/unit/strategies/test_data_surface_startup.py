"""Tests for DataSurfaceManager.start() — eager V4 fetch cold-start fix.

Verifies that start() eagerly fetches /v4/snapshot and handles failures
gracefully (no crash, background loop still starts).
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from strategies.data_surface import DataSurfaceManager


# ── Helpers ──────────────────────────────────────────────────────────


def _v4_snapshot(regime: str = "calm_trend") -> dict:
    """Minimal V4 snapshot payload that passes BTC validation."""
    return {
        "timescales": {
            "5m": {
                "regime": regime,
                "probability_lgb": 0.62,
                "probability_classifier": 0.58,
                "polymarket_live_recommended_outcome": {
                    "direction": "UP",
                    "timing": "optimal",
                    "confidence": 0.65,
                    "trade_advised": True,
                },
            }
        }
    }


# ── Tests ────────────────────────────────────────────────────────────


async def test_startup_fetch_populates_cache():
    """start() eagerly fetches V4 and populates the cache before the loop."""
    mgr = DataSurfaceManager(v4_base_url="http://fake:8080")

    # Mock aiohttp.ClientSession so no real HTTP happens.
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=_v4_snapshot("calm_trend"))

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=AsyncMock())
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        await mgr.start()

    # Cache should be populated for BTC
    assert "BTC" in mgr._cached_v4
    assert mgr._cached_v4["BTC"]["timescales"]["5m"]["regime"] == "calm_trend"

    # Clean up background task
    await mgr.stop()


async def test_startup_fetch_failure_does_not_crash():
    """If the eager fetch raises, start() still completes and the loop runs."""
    mgr = DataSurfaceManager(v4_base_url="http://fake:8080")

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=Exception("connection refused"))
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        # Should NOT raise
        await mgr.start()

    # Background loop should still be running
    assert mgr._running is True
    assert mgr._task is not None

    # Cache should be empty (fetch failed)
    assert mgr._cached_v4.get("BTC") is None

    await mgr.stop()


async def test_startup_fetch_http_error_no_crash():
    """Non-200 from the eager fetch does not prevent startup."""
    mgr = DataSurfaceManager(v4_base_url="http://fake:8080")

    mock_resp = AsyncMock()
    mock_resp.status = 502
    mock_resp.json = AsyncMock(return_value={})

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=AsyncMock())
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        await mgr.start()

    assert mgr._running is True
    # BTC should not be cached (502)
    assert mgr._cached_v4.get("BTC") is None

    await mgr.stop()
