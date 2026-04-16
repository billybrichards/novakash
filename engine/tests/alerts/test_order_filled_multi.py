"""Tests for `TelegramAlerter.send_order_filled` multi-fill (FAK split) rendering.

This module is part of the Telegram redemption visibility plan
(docs/superpowers/plans/2026-04-16-telegram-redemption-visibility.md, Task 5).

A FAK (fill-or-kill) order can split across the layered ask book and produce
multiple `poly_fills` rows for the same `condition_id` at the same timestamp.
The previous `send_order_filled` collapsed those into a single aggregated number,
hiding the split. We now accept an optional `fills` list and render a `🧩 FAK split`
breakdown when len(fills) > 1, while leaving the single-fill output byte-identical.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from alerts.telegram import TelegramAlerter


@pytest.mark.asyncio
async def test_send_order_filled_renders_multi_fill_breakdown():
    a = TelegramAlerter(bot_token="x", chat_id="y")
    a._send_with_id = AsyncMock(return_value=42)
    a._log_notification = AsyncMock()
    # _engine_version is normally set via set_location(); set directly for the test.
    a._engine_version = "v11.2"
    order = MagicMock(direction="NO", stake_usd=4.98, order_id="abc")
    fills = [
        {"price": 0.750, "size": 0.74, "tx": "0x111"},
        {"price": 0.750, "size": 5.90, "tx": "0x222"},
    ]
    msg_id = await a.send_order_filled(
        order, fill_price=0.750, shares=6.64, fills=fills,
    )
    assert msg_id == 42
    text = a._send_with_id.call_args.args[0]
    assert "FAK split" in text
    assert "2 fills" in text
    assert "0.74" in text
    assert "5.90" in text


@pytest.mark.asyncio
async def test_send_order_filled_single_fill_no_split_block():
    """Backwards compat — single fill (or no fills passed) should not show the FAK block."""
    a = TelegramAlerter(bot_token="x", chat_id="y")
    a._send_with_id = AsyncMock(return_value=43)
    a._log_notification = AsyncMock()
    a._engine_version = "v11.2"
    order = MagicMock(direction="NO", stake_usd=4.98, order_id="abc")
    msg_id = await a.send_order_filled(order, fill_price=0.750, shares=6.64)
    assert msg_id == 43
    text = a._send_with_id.call_args.args[0]
    assert "FAK split" not in text
