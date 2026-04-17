"""Regression tests for PolymarketClient.set_paper_mode().

Phantom-trade incident 2026-04-17 (#4881): the prior code mutated
``self.paper_mode`` directly, which left ``_log`` bound to the stale value
and left ``_clob_client`` as a read-only (paper) instance. The new
``set_paper_mode()`` method atomically flips the flag, rebinds the logger,
and invalidates the CLOB client so the next ``connect()`` rebuilds with the
correct auth path.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from execution.polymarket_client import PolymarketClient


def _mk_client(paper_mode: bool = True) -> PolymarketClient:
    return PolymarketClient(
        private_key="0x" + "00" * 32,
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        funder_address="0xOwner",
        paper_mode=paper_mode,
    )


@pytest.mark.asyncio
async def test_set_paper_mode_flips_flag():
    c = _mk_client(paper_mode=True)
    assert c.paper_mode is True
    # LIVE_TRADING_ENABLED is not set for this test, but set_paper_mode
    # itself doesn't enforce that — the enforcement is in __init__ for
    # live construction AND in place_market_order via creds check.
    await c.set_paper_mode(False)
    assert c.paper_mode is False


@pytest.mark.asyncio
async def test_set_paper_mode_invalidates_clob_client():
    """Sentinel handle pre-flip must be dropped so next connect() rebuilds."""
    c = _mk_client(paper_mode=True)
    c._clob_client = object()   # simulate an already-initialised paper client
    await c.set_paper_mode(False)
    assert c._clob_client is None


@pytest.mark.asyncio
async def test_set_paper_mode_rebinds_logger_with_new_mode():
    """``_log`` must reflect the new paper_mode after switch — otherwise
    log lines say paper_mode=True forever even though live orders submit.
    """
    c = _mk_client(paper_mode=True)
    pre_log = c._log
    await c.set_paper_mode(False)
    # A different bound logger instance with the new paper_mode value.
    assert c._log is not pre_log
    # Best-effort check: structlog bound context should now have False.
    ctx = getattr(c._log, "_context", None) or getattr(c._log, "_tmp_context", {})
    if ctx:
        assert ctx.get("paper_mode") is False


@pytest.mark.asyncio
async def test_set_paper_mode_resets_first_trade_warning():
    """After a paper→live flip, the first-live-trade warning banner must
    fire again for user awareness, even if it fired in a prior live cycle.
    """
    c = _mk_client(paper_mode=True)
    c._live_first_trade_warned = True
    await c.set_paper_mode(False)
    assert c._live_first_trade_warned is False


@pytest.mark.asyncio
async def test_set_paper_mode_noop_same_value():
    """Flipping to the same mode should not raise and should leave state
    consistent (idempotent safety)."""
    c = _mk_client(paper_mode=True)
    await c.set_paper_mode(True)
    assert c.paper_mode is True
    # Previous paper _clob_client is invalidated even on noop — caller
    # must always connect() after set_paper_mode(). This is explicit.
    assert c._clob_client is None


@pytest.mark.asyncio
async def test_place_market_order_refuses_live_when_clob_has_no_creds():
    """Pre-submit sanity check — if someone flips to live but forgets to
    call connect(), place_market_order must refuse rather than generate a
    synthetic order_id.
    """
    c = _mk_client(paper_mode=True)
    await c.set_paper_mode(False)
    # Simulate: caller wired a read-only ClobClient (no creds attr truthy).
    fake_ro = MagicMock()
    fake_ro.creds = None
    c._clob_client = fake_ro
    with pytest.raises(RuntimeError, match="creds|read-only|connect"):
        await c.place_market_order(
            token_id="tok-1", price=0.50, size=5.0, order_type="FAK"
        )


@pytest.mark.asyncio
async def test_place_market_order_paper_mode_still_simulates():
    """Paper mode path remains untouched by the creds sanity check."""
    c = _mk_client(paper_mode=True)
    result = await c.place_market_order(
        token_id="tok-1", price=0.50, size=5.0, order_type="FAK"
    )
    assert result["filled"] is True
    assert result["order_id"].startswith("paper-fak-")
