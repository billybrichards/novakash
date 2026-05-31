"""Pool-hardening tripwire tests for DataSurfaceManager.

Today (2026-05-31) the v4/snapshot client wedged for 50+ minutes on Montreal
prod because the aiohttp TCPConnector returned timed-out sockets to the pool
rather than discarding them.  PR #631 fixed the same pattern for the v2/
probability client; this PR ports that fix to the v4 path.

These tests are intentionally lightweight tripwires:
  1. Source-level grep — cheapest possible CI guard that the fix wasn't
     accidentally reverted during a rebase.
  2. Runtime assertion — verifies that start() actually constructs the session
     with force_close and a sock_read timeout (not a total= timeout).

Both levels must pass before merging.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ── 1. Source-level tripwire ──────────────────────────────────────────

_SOURCE = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "..",  # engine/
    "strategies", "data_surface.py",
)


def _source_text() -> str:
    with open(os.path.normpath(_SOURCE)) as fh:
        return fh.read()


def test_force_close_present_in_source():
    """force_close=True must appear in data_surface.py (regression guard)."""
    assert "force_close=True" in _source_text(), (
        "data_surface.py no longer contains force_close=True — "
        "pool-hardening fix may have been reverted."
    )


def test_sock_read_present_in_source():
    """sock_read= must appear instead of total= only timeout (regression guard)."""
    src = _source_text()
    assert "sock_read=" in src, (
        "data_surface.py no longer contains sock_read= — "
        "split-timeout fix may have been reverted."
    )


# ── 2. Runtime assertion ─────────────────────────────────────────────


async def test_session_connector_force_close_at_runtime():
    """start() must build a session whose connector has force_close=True."""
    from strategies.data_surface import DataSurfaceManager

    mgr = DataSurfaceManager(v4_base_url="http://fake-gpu:8080")

    # We need a real (not fully mocked) ClientSession so we can inspect the
    # connector.  Patch _fetch_v4 so no real HTTP fires.
    with patch.object(mgr, "_fetch_v4", new=AsyncMock()):
        await mgr.start()

    try:
        connector = mgr._session.connector
        assert connector is not None, "Session connector is None"
        # aiohttp 3.9+: TCPConnector exposes _force_close (internal) and the
        # public force_close property alias.
        force_close_val = getattr(connector, "force_close", None)
        if force_close_val is None:
            force_close_val = getattr(connector, "_force_close", None)
        assert force_close_val is True, (
            f"connector.force_close is {force_close_val!r}, expected True"
        )
    finally:
        await mgr.stop()


async def test_session_timeout_uses_sock_read_not_total_only():
    """start() must use a split timeout with sock_read set, not just total=."""
    from strategies.data_surface import DataSurfaceManager

    mgr = DataSurfaceManager(v4_base_url="http://fake-gpu:8080")

    with patch.object(mgr, "_fetch_v4", new=AsyncMock()):
        await mgr.start()

    try:
        timeout = mgr._session.timeout
        assert timeout.sock_read is not None, (
            "ClientTimeout.sock_read is None — split-timeout fix missing"
        )
        assert timeout.connect is not None, (
            "ClientTimeout.connect is None — split-timeout fix missing"
        )
    finally:
        await mgr.stop()
