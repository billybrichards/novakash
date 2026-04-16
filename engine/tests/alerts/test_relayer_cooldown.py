"""Tests for TelegramAlerter.send_relayer_cooldown / send_relayer_resumed —
Task 6 of docs/superpowers/plans/2026-04-16-telegram-redemption-visibility.md.

Verifies the new one-shot edge alerts:
  - send_relayer_cooldown renders cooldown card (resets-in, quota, reason)
  - send_relayer_resumed renders the resume card (quota restored)
  - both go through _send_with_id and _log_notification
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from alerts.telegram import TelegramAlerter


@pytest.mark.asyncio
async def test_send_relayer_cooldown_message():
    a = TelegramAlerter(bot_token="x", chat_id="y")
    a._send_with_id = AsyncMock(return_value=11)
    a._log_notification = AsyncMock()
    await a.send_relayer_cooldown(
        {
            "active": True,
            "remaining_seconds": 9906,
            "resets_at": "2026-04-16T13:55:00Z",
            "reason": "quota exceeded: 0 units remaining",
        },
        quota_remaining=0,
        daily_quota_limit=100,
    )
    text = a._send_with_id.call_args.args[0]
    assert "RELAYER COOLDOWN" in text
    assert "2h45m" in text
    assert "0/100" in text


@pytest.mark.asyncio
async def test_send_relayer_resumed_message():
    a = TelegramAlerter(bot_token="x", chat_id="y")
    a._send_with_id = AsyncMock(return_value=12)
    a._log_notification = AsyncMock()
    await a.send_relayer_resumed(quota_remaining=100, daily_quota_limit=100)
    text = a._send_with_id.call_args.args[0]
    assert "RELAYER RESUMED" in text
    assert "100/100" in text
