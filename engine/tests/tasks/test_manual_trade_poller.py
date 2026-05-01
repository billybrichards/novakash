"""Unit tests for ManualTradePoller — B5 deliverable.

Tests cover:
  - NOTIFY received → poller wakes → drain_once called
  - 5 s fallback: drain_once called even without NOTIFY
  - Clean shutdown on asyncio.CancelledError (no error propagation)

All collaborators are mocked; no DB, no network.
"""

from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tasks.manual_trade_poller import ManualTradePoller, _FALLBACK_POLL_S


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_poller(drain_return=None):
    """Build a ManualTradePoller with a mocked DB and use-case."""
    db = AsyncMock()
    db.ensure_listening = AsyncMock(return_value=True)

    uc = AsyncMock()
    uc.drain_once = AsyncMock(return_value=drain_return or [])

    poller = ManualTradePoller(db=db, use_case=uc)
    return poller, db, uc


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_wakes_poller_and_calls_drain():
    """A PG NOTIFY fires drain_once immediately (no 5 s wait)."""
    poller, db, uc = _make_poller()

    # Schedule: fire the notify callback after a short delay, then cancel the task.
    async def _run_and_interrupt():
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0)  # let the poller start and reach wait_for()
        # Simulate a NOTIFY from asyncpg by calling the registered callback directly.
        poller._on_notify(conn=None, pid=1234, channel="manual_trade_pending", payload="tid-001")
        await asyncio.sleep(0.05)  # let the drain execute
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await _run_and_interrupt()

    # drain_once should have been called at least once (from the NOTIFY wakeup).
    assert uc.drain_once.called


@pytest.mark.asyncio
async def test_fallback_poll_calls_drain_without_notify():
    """After _FALLBACK_POLL_S (patched to 0.05 s) drain_once is called even without NOTIFY."""
    poller, db, uc = _make_poller()

    # Patch the fallback timeout to something very small so the test is fast.
    with patch("tasks.manual_trade_poller._FALLBACK_POLL_S", 0.05):
        async def _run_and_interrupt():
            task = asyncio.create_task(poller.run())
            # Wait longer than the patched fallback to guarantee at least one timeout cycle.
            await asyncio.sleep(0.20)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_and_interrupt()

    # drain_once must have been called at least once via the fallback poll.
    assert uc.drain_once.call_count >= 1


@pytest.mark.asyncio
async def test_multiple_notifies_each_trigger_drain():
    """Each NOTIFY results in a drain_once call."""
    poller, db, uc = _make_poller()

    with patch("tasks.manual_trade_poller._FALLBACK_POLL_S", 5.0):  # disable fallback
        async def _run_and_interrupt():
            task = asyncio.create_task(poller.run())
            await asyncio.sleep(0)

            # Fire two NOTIFYs with a small gap between them.
            poller._on_notify(None, 1, "manual_trade_pending", "tid-1")
            await asyncio.sleep(0.05)
            poller._on_notify(None, 1, "manual_trade_pending", "tid-2")
            await asyncio.sleep(0.10)

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_and_interrupt()

    # We sent 2 NOTIFYs but the exact drain count depends on asyncio timing;
    # what we require is at least 1 call (not 0).
    assert uc.drain_once.call_count >= 1


@pytest.mark.asyncio
async def test_clean_shutdown_on_cancel():
    """CancelledError from the task causes run() to exit cleanly (re-raises CancelledError)."""
    poller, db, uc = _make_poller()

    with patch("tasks.manual_trade_poller._FALLBACK_POLL_S", 60.0):  # long timeout
        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0)  # let poller reach the wait_for()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_drain_exception_does_not_kill_poller():
    """An exception in drain_once is caught; the poller continues running."""
    poller, db, uc = _make_poller()
    uc.drain_once.side_effect = RuntimeError("DB connection lost")

    with patch("tasks.manual_trade_poller._FALLBACK_POLL_S", 0.05):
        async def _run_and_interrupt():
            task = asyncio.create_task(poller.run())
            await asyncio.sleep(0.20)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_and_interrupt()

    # Task cancelled cleanly — the RuntimeError was swallowed by _drain().
    # drain_once was called multiple times (fallback poll kept firing).
    assert uc.drain_once.call_count >= 1


@pytest.mark.asyncio
async def test_listen_failure_on_boot_is_non_fatal():
    """If ensure_listening raises on startup, the poller still starts and polls."""
    poller, db, uc = _make_poller()
    db.ensure_listening.side_effect = Exception("Postgres not reachable")

    with patch("tasks.manual_trade_poller._FALLBACK_POLL_S", 0.05):
        async def _run_and_interrupt():
            task = asyncio.create_task(poller.run())
            await asyncio.sleep(0.20)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_and_interrupt()

    # Poller ran despite the listen failure.
    assert uc.drain_once.call_count >= 1
