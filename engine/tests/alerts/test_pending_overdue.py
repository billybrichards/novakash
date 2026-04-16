"""Task #198 — Pending-wins-overdue Telegram alert.

Verifies:
  - ``TelegramAlerter.send_pending_overdue`` renders the expected card and
    routes through ``_send_with_id`` + ``_log_notification``.
  - Only overdue positions (``overdue_seconds > 1800``) appear in the list.
  - The orchestrator helper ``_maybe_send_pending_overdue_alert`` fires when
    BOTH thresholds are crossed (any position > 30 min AND total > $30).
  - The orchestrator helper DEDUPES within a 60-min window.
  - The orchestrator helper DOES NOT fire when only one threshold is crossed
    (e.g. total > $30 but no single position > 30 min, or vice versa).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from alerts.telegram import TelegramAlerter


# ── Telegram renderer tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_pending_overdue_renders_card():
    a = TelegramAlerter(bot_token="x", chat_id="y")
    a._send_with_id = AsyncMock(return_value=101)
    a._log_notification = AsyncMock()

    wins = [
        {
            "condition_id": "0xaaaaaaaaaaaaaaaa",
            "value": 7.40,
            "overdue_seconds": 3600,  # 60m
        },
        {
            "condition_id": "0xbbbbbbbbbbbbbbbb",
            "value": 25.50,
            "overdue_seconds": 2500,  # 41m
        },
    ]
    await a.send_pending_overdue(pending_wins=wins, pending_total_usd=32.90)

    a._send_with_id.assert_awaited_once()
    text = a._send_with_id.call_args.args[0]
    assert "PENDING WINS OVERDUE" in text
    assert "$32.90" in text
    assert "`2` overdue" in text
    # Both condition_ids appear (truncated)
    assert "0xaaaaaaaa" in text
    assert "0xbbbbbbbb" in text
    # Newest-stuck rendered first (60m > 41m)
    assert text.index("0xaaaaaaaa") < text.index("0xbbbbbbbb")
    # Ages show up
    assert "1h0m" in text  # 60m formatted as 1h0m
    assert "41m" in text
    # Log recorded under the "pending_overdue" key
    a._log_notification.assert_awaited_once()
    assert a._log_notification.call_args.args[0] == "pending_overdue"


@pytest.mark.asyncio
async def test_send_pending_overdue_filters_non_overdue():
    """Positions with overdue_seconds <= 1800 must NOT appear in the list
    even if caller passes them (defensive — matches the fire condition)."""
    a = TelegramAlerter(bot_token="x", chat_id="y")
    a._send_with_id = AsyncMock(return_value=102)
    a._log_notification = AsyncMock()

    wins = [
        {"condition_id": "0xoverdue", "value": 10.0, "overdue_seconds": 2000},
        {"condition_id": "0xfresh", "value": 5.0, "overdue_seconds": 300},
        {"condition_id": "0xboundary", "value": 5.0, "overdue_seconds": 1800},  # == threshold, excluded
    ]
    await a.send_pending_overdue(pending_wins=wins, pending_total_usd=20.0)
    text = a._send_with_id.call_args.args[0]

    assert "0xoverdue" in text
    assert "0xfresh" not in text
    assert "0xboundary" not in text
    assert "`1` overdue" in text


@pytest.mark.asyncio
async def test_send_pending_overdue_caps_at_10_rows():
    """Spec says "list (up to 10) overdue positions" — 11th shows as a
    +N footer."""
    a = TelegramAlerter(bot_token="x", chat_id="y")
    a._send_with_id = AsyncMock(return_value=103)
    a._log_notification = AsyncMock()

    wins = [
        {
            "condition_id": f"0x{'0' * 15}{i:x}",
            "value": 5.0,
            "overdue_seconds": 2000 + i,
        }
        for i in range(15)
    ]
    await a.send_pending_overdue(pending_wins=wins, pending_total_usd=75.0)

    text = a._send_with_id.call_args.args[0]
    assert "+5 more" in text  # 15 - 10


# ── Orchestrator helper tests ─────────────────────────────────────────────────


def _make_runtime_with_helper():
    """Construct an EngineRuntime instance bypassing __init__, with just
    the attrs ``_maybe_send_pending_overdue_alert`` needs. Keeps tests
    focused on Task #198 plumbing without bootstrapping the full engine.
    """
    from infrastructure.runtime import EngineRuntime

    o = EngineRuntime.__new__(EngineRuntime)
    o._alerter = MagicMock()
    o._alerter.send_pending_overdue = AsyncMock(return_value=1)
    o._last_pending_overdue_alert_at = None
    return o


@pytest.mark.asyncio
async def test_overdue_alert_fires_when_both_thresholds_crossed():
    o = _make_runtime_with_helper()
    pending = [
        {"condition_id": "0xa", "value": 40.0, "overdue_seconds": 3600},  # 60m
    ]
    snap = {"pending_total_usd": 40.0}

    await o._maybe_send_pending_overdue_alert(pending, snap)

    o._alerter.send_pending_overdue.assert_awaited_once()
    assert o._last_pending_overdue_alert_at is not None


@pytest.mark.asyncio
async def test_overdue_alert_does_not_fire_when_total_under_30():
    """Single position over 30 min but aggregate ≤ $30 — no alert."""
    o = _make_runtime_with_helper()
    pending = [
        {"condition_id": "0xa", "value": 20.0, "overdue_seconds": 3600},
    ]
    snap = {"pending_total_usd": 20.0}

    await o._maybe_send_pending_overdue_alert(pending, snap)

    o._alerter.send_pending_overdue.assert_not_awaited()
    assert o._last_pending_overdue_alert_at is None


@pytest.mark.asyncio
async def test_overdue_alert_does_not_fire_when_no_position_over_30min():
    """Total > $30 but every position is < 30m — no alert (still inside
    NegRisk's normal SLA). Prevents false positives during a cluster of
    fresh resolutions."""
    o = _make_runtime_with_helper()
    pending = [
        {"condition_id": "0xa", "value": 20.0, "overdue_seconds": 600},
        {"condition_id": "0xb", "value": 20.0, "overdue_seconds": 1000},
    ]
    snap = {"pending_total_usd": 40.0}

    await o._maybe_send_pending_overdue_alert(pending, snap)

    o._alerter.send_pending_overdue.assert_not_awaited()


@pytest.mark.asyncio
async def test_overdue_alert_dedupes_within_60_min():
    """Back-to-back snapshot ticks with the same overdue set must only
    produce ONE Telegram alert — hourly cadence, not per-tick spam."""
    o = _make_runtime_with_helper()
    pending = [
        {"condition_id": "0xa", "value": 40.0, "overdue_seconds": 3600},
    ]
    snap = {"pending_total_usd": 40.0}

    # First call fires
    await o._maybe_send_pending_overdue_alert(pending, snap)
    assert o._alerter.send_pending_overdue.await_count == 1

    # Second call (immediately after) is suppressed
    await o._maybe_send_pending_overdue_alert(pending, snap)
    assert o._alerter.send_pending_overdue.await_count == 1

    # Third — simulate 30 min passing, still suppressed
    o._last_pending_overdue_alert_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    await o._maybe_send_pending_overdue_alert(pending, snap)
    assert o._alerter.send_pending_overdue.await_count == 1


@pytest.mark.asyncio
async def test_overdue_alert_re_fires_after_dedup_window():
    """After 60 min, the alert is eligible to fire again if thresholds
    still hold — gives the operator an hourly reminder for persistent
    NegRisk slowdowns."""
    o = _make_runtime_with_helper()
    pending = [
        {"condition_id": "0xa", "value": 40.0, "overdue_seconds": 4000},
    ]
    snap = {"pending_total_usd": 40.0}

    # Seed the dedup timestamp just OVER 60 min ago
    o._last_pending_overdue_alert_at = datetime.now(timezone.utc) - timedelta(minutes=61)

    await o._maybe_send_pending_overdue_alert(pending, snap)

    o._alerter.send_pending_overdue.assert_awaited_once()


@pytest.mark.asyncio
async def test_overdue_alert_no_alerter_is_noop():
    """Defensive: missing alerter must not raise."""
    from infrastructure.runtime import EngineRuntime

    o = EngineRuntime.__new__(EngineRuntime)
    o._alerter = None
    o._last_pending_overdue_alert_at = None
    pending = [{"condition_id": "0xa", "value": 40.0, "overdue_seconds": 3600}]
    snap = {"pending_total_usd": 40.0}

    await o._maybe_send_pending_overdue_alert(pending, snap)  # must not raise
    assert o._last_pending_overdue_alert_at is None
