"""Test for TelegramAlerter.send_position_snapshot — Task 3 of
docs/superpowers/plans/2026-04-16-telegram-redemption-visibility.md.

Verifies the new method:
  - renders the snapshot via alerts.positions.render_snapshot_text
  - sends via _send_with_id and returns the resulting Telegram message_id
  - logs the notification with type "position_snapshot" (canonical key
    consumed by Hub /api/notifications + FE filter).
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from alerts.telegram import TelegramAlerter


@pytest.mark.asyncio
async def test_send_position_snapshot_logs_notification():
    a = TelegramAlerter(bot_token="x", chat_id="y")
    a._send_with_id = AsyncMock(return_value=999)
    a._log_notification = AsyncMock()
    snap = {
        "now_utc": "2026-04-16T11:10:00Z",
        "wallet_usdc": 135.57,
        "pending_wins": [],
        "pending_count": 0,
        "pending_total_usd": 0.0,
        "overdue_count": 0,
        "effective_balance": 135.57,
        "open_orders": [],
        "open_orders_count": 0,
        "cooldown": {
            "active": False,
            "remaining_seconds": 0,
            "resets_at": None,
            "reason": "",
        },
        "daily_quota_limit": 100,
        "quota_used_today": 0,
        "quota_remaining": 100,
    }
    msg_id = await a.send_position_snapshot(snap)
    assert msg_id == 999
    args, kwargs = a._log_notification.call_args
    assert args[0] == "position_snapshot"
    assert "POSITION SNAPSHOT" in args[1]
