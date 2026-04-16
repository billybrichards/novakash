"""
Task 2 — Redeemer exposes pending-wins summary.

Verifies `PositionRedeemer.pending_wins_summary()` returns the redeemable
positions (read from `_scan_redeemable_positions()`) annotated with
`overdue_seconds` and sorted oldest-first (worst overdue at the top).

This is the data source that the position-snapshot Telegram alert and the
Hub /api/positions/snapshot endpoint feed into the pure
`build_snapshot()` builder added in Task 1.

The test mocks `_scan_redeemable_positions()` so it runs without any
network or web3 setup, and proves the method is read-only (no relayer
call, no quota burn).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from execution.redeemer import PositionRedeemer


@pytest.mark.asyncio
async def test_pending_wins_summary_marks_overdue(monkeypatch):
    # paper_mode=True keeps the constructor light — no relay client init.
    # The remaining required ctor args take placeholder strings; nothing
    # in pending_wins_summary() touches them because we mock the scan.
    r = PositionRedeemer(
        rpc_url="https://test.invalid",
        private_key="0x" + "0" * 64,
        proxy_address="0x" + "0" * 40,
        paper_mode=True,
    )
    now = datetime(2026, 4, 16, 11, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        r,
        "_scan_redeemable_positions",
        AsyncMock(
            return_value=[
                {
                    "condition_id": "0xaaa",
                    "value": 7.40,
                    "resolved_at": now - timedelta(minutes=95),
                },
                {
                    "condition_id": "0xbbb",
                    "value": 4.50,
                    "resolved_at": now - timedelta(minutes=2),
                },
            ]
        ),
    )

    summary, scan_ok = await r.pending_wins_summary(now=now)

    assert scan_ok is True
    assert len(summary) == 2
    # Sorted oldest-first (worst overdue at top)
    assert summary[0]["condition_id"] == "0xaaa"
    assert summary[0]["overdue_seconds"] == 5700  # 95 min × 60
    assert summary[1]["condition_id"] == "0xbbb"
    assert summary[1]["overdue_seconds"] == 120

    # Shape contract — keys consumed by build_snapshot() in Task 1.
    for row in summary:
        assert set(row.keys()) >= {
            "condition_id",
            "value",
            "window_end_utc",
            "overdue_seconds",
        }
        assert isinstance(row["value"], float)
    # window_end_utc is the ISO-8601 string of resolved_at
    assert summary[0]["window_end_utc"] == (now - timedelta(minutes=95)).isoformat()


@pytest.mark.asyncio
async def test_pending_wins_summary_uses_real_endDate_from_fetch(monkeypatch):
    """Integration: confirm endDate flows through fetch_redeemable_positions
    into _scan_redeemable_positions into pending_wins_summary, so OVERDUE
    actually fires in production. Regression guard for the original bug
    where fetch_redeemable_positions stripped endDate before downstream
    code could see it."""
    r = PositionRedeemer(
        rpc_url="https://test.invalid",
        private_key="0x" + "0" * 64,
        proxy_address="0x" + "0" * 40,
        paper_mode=True,
    )
    now = datetime(2026, 4, 16, 11, 10, 0, tzinfo=timezone.utc)
    fake_position_row = {
        "conditionId": "0xfeed",
        "size": 10.0,
        "avgPrice": 0.55,
        "curPrice": 1.0,
        "outcome": "WIN",
        "pnl": 4.50,
        "tokenId": "12345",
        "asset": "12345",
        # Polymarket data-api emits ISO-8601 strings under `endDate`
        # (see _scan_redeemable_positions which reads `r.get("endDate")`).
        "endDate": "2026-04-16T10:10:00Z",  # 60 minutes before `now`
    }
    monkeypatch.setattr(
        r,
        "fetch_redeemable_positions",
        AsyncMock(return_value=[fake_position_row]),
    )

    summary, scan_ok = await r.pending_wins_summary(now=now)

    assert scan_ok is True
    assert len(summary) == 1
    assert summary[0]["condition_id"] == "0xfeed"
    assert summary[0]["overdue_seconds"] == 3600  # exactly 60 min



@pytest.mark.asyncio
async def test_pending_wins_summary_returns_scan_failed_on_exception(monkeypatch):
    """Audit #204 regression: scan exception MUST return ([], False) so
    callers know the empty list means "unknown", not "no pending wins".

    Previously returned [] unconditionally → upsert_pending_wins([])
    wiped poly_pending_wins → Hub snapshot reported 0 pending even
    though wallet had 14 overdue wins. Observed in prod 2026-04-16.
    """
    r = PositionRedeemer(
        rpc_url="https://test.invalid",
        private_key="0x" + "0" * 64,
        proxy_address="0x" + "0" * 40,
        paper_mode=True,
    )
    now = datetime(2026, 4, 16, 15, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        r,
        "_scan_redeemable_positions",
        AsyncMock(side_effect=RuntimeError("data-api 429 cooldown")),
    )

    summary, scan_ok = await r.pending_wins_summary(now=now)

    assert summary == []
    assert scan_ok is False, "scan_failure must yield scan_successful=False"

