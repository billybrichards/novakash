"""Test for EngineRuntime._send_position_snapshot — Task 4 of
docs/superpowers/plans/2026-04-16-telegram-redemption-visibility.md.

Wires mocked redeemer + alerter + poly_client + db into the runtime,
calls _send_position_snapshot once, and confirms a snapshot dict was
sent with the expected fields populated.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_send_position_snapshot_builds_and_sends():
    # Local import to keep collection cheap if runtime imports break
    from infrastructure.runtime import EngineRuntime

    o = EngineRuntime.__new__(EngineRuntime)  # bypass __init__
    o._shutdown_event = MagicMock()
    o._alerter = MagicMock()
    o._alerter.send_position_snapshot = AsyncMock(return_value=42)

    o._redeemer = MagicMock()
    o._redeemer.pending_wins_summary = AsyncMock(return_value=[
        {
            "condition_id": "0xa",
            "value": 5.0,
            "window_end_utc": "2026-04-16T11:00:00Z",
            "overdue_seconds": 600,
        },
    ])
    o._redeemer.cooldown_status = MagicMock(return_value={
        "active": False,
        "remaining_seconds": 0,
        "resets_at": None,
        "reason": "",
    })
    o._redeemer.daily_quota_limit = 100

    o._db = MagicMock()
    o._db.count_redeems_today = AsyncMock(return_value=7)

    o._poly_client = MagicMock()
    o._poly_client.get_balance = AsyncMock(return_value=135.57)
    o._poly_client.get_open_orders = AsyncMock(return_value=[])

    await o._send_position_snapshot()

    o._alerter.send_position_snapshot.assert_awaited_once()
    snap = o._alerter.send_position_snapshot.await_args.args[0]
    assert snap["wallet_usdc"] == 135.57
    assert snap["pending_count"] == 1
    assert snap["overdue_count"] == 1
    assert snap["effective_balance"] == 140.57
    assert snap["quota_remaining"] == 93
