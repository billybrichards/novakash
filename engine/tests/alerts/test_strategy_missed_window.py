"""Tests for TelegramAlerter.send_strategy_missed_window — Task 7.5 of
docs/superpowers/plans/2026-04-16-telegram-redemption-visibility.md.

Verifies the new loud alert that fires when a LIVE strategy is never
evaluated inside its eligible window. The alert carries enough diagnostic
context (sibling-eval count, first-eval offset) to triage in 5 seconds.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from alerts.telegram import TelegramAlerter


@pytest.mark.asyncio
async def test_send_strategy_missed_window_loud_alert():
    a = TelegramAlerter(bot_token="x", chat_id="y")
    a._send_with_id = AsyncMock(return_value=77)
    a._log_notification = AsyncMock()
    await a.send_strategy_missed_window(
        strategy_id="v4_fusion",
        mode="LIVE",
        window_ts=1745842200,
        bounds_str="T-180..T-70",
        siblings_evaluated=3,
        siblings_total=5,
        first_eval_offset=60,
    )
    text = a._send_with_id.call_args.args[0]
    assert "STRATEGY MISSED WINDOW" in text
    assert "v4_fusion" in text
    assert "LIVE" in text
    assert "T-180..T-70" in text
    assert "3/5" in text  # sibling evaluation count
    args, kwargs = a._log_notification.call_args
    assert args[0] == "strategy_missed_window"
