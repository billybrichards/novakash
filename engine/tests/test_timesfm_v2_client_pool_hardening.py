"""Pool-hardening tripwire tests for TimesFMV2Client.

Mirrors PR #638's tests for the v4/snapshot client (data_surface.py:838)
onto the v2/probability persistent ClientSession in
engine/adapters/prediction/timesfm_v2.py.

Per RDS notes #780 / #824 the latent connection-pool leak risk on the
v2/probability client was never patched: PR #631 added retries but the
ClientSession itself was still built with a default TCPConnector (no
``force_close``, no ``enable_cleanup_closed``). Under sustained upstream
slowness, sockets return to the pool in a wedged keepalive state and
subsequent requests time out 1:1 with wallclock. This file is the
regression guard against that pattern being reverted.

Both tests must pass before merging.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── 1. Source-level tripwire ──────────────────────────────────────────

_SOURCE = os.path.join(
    os.path.dirname(__file__),
    "..",  # engine/
    "adapters",
    "prediction",
    "timesfm_v2.py",
)


def _source_text() -> str:
    with open(os.path.normpath(_SOURCE)) as fh:
        return fh.read()


def test_force_close_present_in_source():
    """force_close=True must appear in timesfm_v2.py (regression guard)."""
    assert "force_close=True" in _source_text(), (
        "timesfm_v2.py no longer contains force_close=True — "
        "pool-hardening fix may have been reverted."
    )


def test_enable_cleanup_closed_present_in_source():
    """enable_cleanup_closed=True must appear in timesfm_v2.py (regression guard)."""
    assert "enable_cleanup_closed=True" in _source_text(), (
        "timesfm_v2.py no longer contains enable_cleanup_closed=True — "
        "pool-hardening fix may have been reverted."
    )


# ── 2. Runtime assertion ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_connector_force_close_at_runtime():
    """_get_session() must build a session whose connector has force_close=True."""
    from signals.timesfm_v2_client import TimesFMV2Client

    client = TimesFMV2Client(base_url="http://fake-gpu:8001")

    session = await client._get_session()
    try:
        connector = session.connector
        assert connector is not None, "Session connector is None"
        # aiohttp 3.9+: TCPConnector exposes force_close (public) and
        # _force_close (internal). Accept either.
        force_close_val = getattr(connector, "force_close", None)
        if force_close_val is None:
            force_close_val = getattr(connector, "_force_close", None)
        assert force_close_val is True, (
            f"connector.force_close is {force_close_val!r}, expected True"
        )
    finally:
        await session.close()
