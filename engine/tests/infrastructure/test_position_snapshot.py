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
    # Audit #204: returns (list, scan_successful) tuple.
    o._redeemer.pending_wins_summary = AsyncMock(return_value=([
        {
            "condition_id": "0xa",
            "value": 5.0,
            "window_end_utc": "2026-04-16T11:00:00Z",
            "overdue_seconds": 600,
        },
    ], True))
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


@pytest.mark.asyncio
async def test_send_position_snapshot_warns_on_wallet_failure(monkeypatch):
    """Wallet RPC failure must surface as a WARN log, not silent zeros."""
    from infrastructure import runtime as runtime_mod
    from infrastructure.runtime import EngineRuntime

    # structlog uses PrintLoggerFactory (not stdlib), so caplog won't capture.
    # Patch the module-level `log.warning` with a MagicMock and assert calls.
    warn_mock = MagicMock()
    monkeypatch.setattr(runtime_mod.log, "warning", warn_mock)

    o = EngineRuntime.__new__(EngineRuntime)
    o._shutdown_event = MagicMock()
    o._alerter = MagicMock()
    o._alerter.send_position_snapshot = AsyncMock(return_value=1)
    o._redeemer = MagicMock()
    o._redeemer.pending_wins_summary = AsyncMock(return_value=([], True))
    o._redeemer.cooldown_status = MagicMock(return_value={
        "active": False,
        "remaining_seconds": 0,
        "resets_at": None,
        "reason": "",
    })
    o._redeemer.daily_quota_limit = 100
    o._db = MagicMock()
    o._db.count_redeems_today = AsyncMock(return_value=0)
    o._poly_client = MagicMock()
    o._poly_client.get_balance = AsyncMock(side_effect=RuntimeError("rpc down"))
    o._poly_client.get_open_orders = AsyncMock(return_value=[])

    await o._send_position_snapshot()

    # Snapshot still fired (degraded but not silent)
    o._alerter.send_position_snapshot.assert_awaited_once()
    snap = o._alerter.send_position_snapshot.await_args.args[0]
    assert snap["wallet_usdc"] == 0.0

    # And the failure was logged loudly with the expected event key
    events = [c.args[0] for c in warn_mock.call_args_list if c.args]
    assert "snapshot.wallet_balance_failed" in events, (
        f"expected snapshot.wallet_balance_failed WARN, got events={events}"
    )


@pytest.mark.asyncio
async def test_send_position_snapshot_persists_to_db():
    """Snapshot loop must call upsert_pending_wins + upsert_redeemer_state."""
    from infrastructure.runtime import EngineRuntime

    o = EngineRuntime.__new__(EngineRuntime)
    o._shutdown_event = MagicMock()
    o._alerter = MagicMock()
    o._alerter.send_position_snapshot = AsyncMock(return_value=1)
    o._redeemer = MagicMock()
    # Audit #204: returns (list, scan_successful) tuple.
    o._redeemer.pending_wins_summary = AsyncMock(return_value=([
        {
            "condition_id": "0xa",
            "value": 5.0,
            "window_end_utc": "2026-04-16T11:00:00Z",
            "overdue_seconds": 600,
        },
    ], True))
    o._redeemer.cooldown_status = MagicMock(return_value={
        "active": False,
        "remaining_seconds": 0,
        "resets_at": None,
        "reason": "",
    })
    o._redeemer.daily_quota_limit = 100
    o._db = MagicMock()
    o._db.count_redeems_today = AsyncMock(return_value=7)
    o._db.upsert_pending_wins = AsyncMock()
    o._db.upsert_redeemer_state = AsyncMock()
    o._poly_client = MagicMock()
    o._poly_client.get_balance = AsyncMock(return_value=135.0)
    o._poly_client.get_open_orders = AsyncMock(return_value=[])

    await o._send_position_snapshot()

    o._db.upsert_pending_wins.assert_awaited_once()
    # Audit #204: scan_successful MUST be propagated to the DB writer so
    # a transient scan failure preserves the existing snapshot rather
    # than wiping it. Happy path passes scan_successful=True.
    call = o._db.upsert_pending_wins.await_args
    assert call.kwargs.get("scan_successful") is True, (
        "upsert_pending_wins must receive scan_successful=True on good scan"
    )
    o._db.upsert_redeemer_state.assert_awaited_once_with(
        {"active": False, "remaining_seconds": 0, "resets_at": None, "reason": ""},
        100, 7,
    )


@pytest.mark.asyncio
async def test_send_position_snapshot_scan_failure_preserves_db():
    """Audit #204: scan failure must NOT wipe the DB snapshot.

    Regression guard for the 14-pending → 0-pending prod incident on
    2026-04-16. Wallet did not move, no redemption happened, yet the
    snapshot was wiped because pending_wins_summary() returned [] on
    a transient data-api 429 and upsert_pending_wins([]) did
    DELETE+INSERT-nothing = wipe.

    Fix: pending_wins_summary now returns (list, scan_successful).
    Runtime propagates scan_successful to upsert_pending_wins, which
    skips the DELETE entirely when False.
    """
    from infrastructure.runtime import EngineRuntime

    o = EngineRuntime.__new__(EngineRuntime)
    o._shutdown_event = MagicMock()
    o._alerter = MagicMock()
    o._alerter.send_position_snapshot = AsyncMock(return_value=1)
    o._redeemer = MagicMock()
    # Scan fails → empty list + scan_successful=False.
    o._redeemer.pending_wins_summary = AsyncMock(return_value=([], False))
    o._redeemer.cooldown_status = MagicMock(return_value={
        "active": False, "remaining_seconds": 0, "resets_at": None, "reason": "",
    })
    o._redeemer.daily_quota_limit = 80
    o._db = MagicMock()
    o._db.count_redeems_today = AsyncMock(return_value=7)
    o._db.upsert_pending_wins = AsyncMock()
    o._db.upsert_redeemer_state = AsyncMock()
    o._poly_client = MagicMock()
    o._poly_client.get_balance = AsyncMock(return_value=83.31)
    o._poly_client.get_open_orders = AsyncMock(return_value=[])

    await o._send_position_snapshot()

    # upsert_pending_wins MUST still be called (it needs the flag to
    # branch internally), but with scan_successful=False.
    o._db.upsert_pending_wins.assert_awaited_once()
    call = o._db.upsert_pending_wins.await_args
    assert call.kwargs.get("scan_successful") is False, (
        "scan failure MUST propagate scan_successful=False to DB writer "
        "(otherwise DELETE FROM poly_pending_wins wipes the snapshot)"
    )
