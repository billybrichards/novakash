"""Tests for the pure position-snapshot builder + Telegram text renderer.

This module is part of the Telegram redemption visibility plan
(docs/superpowers/plans/2026-04-16-telegram-redemption-visibility.md, Task 1).
The same dict shape returned by `build_snapshot` is later consumed by the
Hub HTTP endpoint and the React frontend bar — locking the contract here.
"""
from __future__ import annotations

from alerts.positions import build_snapshot, render_snapshot_text


def test_build_snapshot_computes_effective_balance():
    snap = build_snapshot(
        wallet_usdc=135.57,
        pending_wins=[
            {
                "condition_id": "0xaaa",
                "value": 7.40,
                "window_end_utc": "2026-04-16T09:35:00Z",
                "overdue_seconds": 5700,
            },
            {
                "condition_id": "0xbbb",
                "value": 7.18,
                "window_end_utc": "2026-04-16T09:45:00Z",
                "overdue_seconds": 5100,
            },
        ],
        open_orders=[],
        cooldown={
            "active": False,
            "remaining_seconds": 0,
            "resets_at": None,
            "reason": "",
        },
        daily_quota_limit=100,
        quota_used_today=12,
        now_utc="2026-04-16T11:10:00Z",
    )
    assert snap["wallet_usdc"] == 135.57
    assert snap["pending_total_usd"] == 14.58
    assert snap["effective_balance"] == 150.15
    assert snap["pending_count"] == 2
    assert snap["overdue_count"] == 2  # both > 5min past window_end
    assert snap["cooldown"]["active"] is False
    assert snap["quota_remaining"] == 88


def test_render_snapshot_text_marks_overdue_wins():
    snap = build_snapshot(
        wallet_usdc=135.57,
        pending_wins=[
            {
                "condition_id": "0xaaa",
                "value": 7.40,
                "window_end_utc": "2026-04-16T09:35:00Z",
                "overdue_seconds": 5700,
            },
        ],
        open_orders=[],
        cooldown={
            "active": False,
            "remaining_seconds": 0,
            "resets_at": None,
            "reason": "",
        },
        daily_quota_limit=100,
        quota_used_today=0,
        now_utc="2026-04-16T11:10:00Z",
    )
    text = render_snapshot_text(snap)
    assert "$135.57" in text
    assert "$142.97" in text  # effective
    assert "1 pending" in text
    assert "OVERDUE" in text  # >5min past window_end


def test_overdue_boundary_is_strict_greater_than():
    base = {
        "wallet_usdc": 100.0,
        "open_orders": [],
        "cooldown": {"active": False, "remaining_seconds": 0, "resets_at": None, "reason": ""},
        "daily_quota_limit": 100,
        "quota_used_today": 0,
        "now_utc": "2026-04-16T11:10:00Z",
    }
    snap_at = build_snapshot(
        pending_wins=[{"condition_id": "0xa", "value": 1.0, "window_end_utc": "x", "overdue_seconds": 300}],
        **base,
    )
    snap_over = build_snapshot(
        pending_wins=[{"condition_id": "0xb", "value": 1.0, "window_end_utc": "x", "overdue_seconds": 301}],
        **base,
    )
    assert snap_at["overdue_count"] == 0   # exactly 300s = NOT overdue
    assert snap_over["overdue_count"] == 1  # 301s = overdue


def test_render_snapshot_text_shows_cooldown_when_active():
    snap = build_snapshot(
        wallet_usdc=200.0,
        pending_wins=[],
        open_orders=[],
        cooldown={
            "active": True,
            "remaining_seconds": 1800,
            "resets_at": "2026-04-16T11:40:00Z",
            "reason": "429 quota exceeded",
        },
        daily_quota_limit=100,
        quota_used_today=100,
        now_utc="2026-04-16T11:10:00Z",
    )
    text = render_snapshot_text(snap)
    assert "RELAYER COOLDOWN" in text
    assert "30m" in text  # 1800s formatted as 30m
    assert "0/100 quota left" in text
