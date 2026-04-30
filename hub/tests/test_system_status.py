"""
Hub /api/system/status mode-derivation tests.

The endpoint adds a derived `mode` field (LIVE / PAPER / KILLED / UNKNOWN)
the FE renders directly. These tests cover every precedence branch in
api.system._derive_mode without booting an HTTP client — _derive_mode is
deliberately written as a pure function so the test stays simple.

Fixtures mirror the actual writers:
  - hub: `/api/system/kill` writes ``state.kill_switch_manual``, etc.
  - engine: ``publish_heartbeat`` writes ``engine_status="running"``
    column + ``config`` jsonb with ``kill_switch_active`` and
    ``paper_mode`` nested.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.system import _derive_mode


def _row(state=None, config=None, engine_status=None, paper_enabled=None):
    """Build a SystemState lookalike with the same attributes the model exposes."""
    return SimpleNamespace(
        state=state,
        config=config,
        engine_status=engine_status,
        paper_enabled=paper_enabled,
    )


# ── No / empty state ────────────────────────────────────────────────────────


def test_mode_unknown_when_no_state_row():
    assert _derive_mode(None) == "UNKNOWN"


def test_mode_unknown_when_state_blob_empty():
    assert _derive_mode(_row(state={}, config={})) == "UNKNOWN"


def test_mode_unknown_when_state_is_not_a_dict():
    # Defensive: if the jsonb came back as a string / list / None we
    # collapse to UNKNOWN instead of crashing the whole endpoint.
    assert _derive_mode(_row(state="oops", config=None)) == "UNKNOWN"
    assert _derive_mode(_row(state=[1, 2], config=None)) == "UNKNOWN"


# ── KILLED precedence (highest) ─────────────────────────────────────────────


def test_mode_killed_via_hub_manual_kill():
    # Hub /api/system/kill writes this flag into state jsonb.
    row = _row(
        state={"kill_switch_manual": True, "paper_mode": True},
        config={"paper_mode": False, "kill_switch_active": False},
        engine_status="running",
    )
    assert _derive_mode(row) == "KILLED"


def test_mode_killed_via_engine_auto_drawdown():
    # Engine writes kill_switch_active inside config jsonb when the
    # auto-drawdown kill switch trips.
    row = _row(
        state={},
        config={"kill_switch_active": True, "paper_mode": False},
        engine_status="running",
    )
    assert _derive_mode(row) == "KILLED"


def test_mode_killed_legacy_auto_field_in_state_jsonb():
    # Older hub builds set kill_switch_auto in state — keep handling it.
    row = _row(
        state={"kill_switch_auto": True},
        config={},
        engine_status="running",
    )
    assert _derive_mode(row) == "KILLED"


# ── PAPER precedence (after KILLED) ─────────────────────────────────────────


def test_mode_paper_when_hub_state_paper_mode_set():
    row = _row(
        state={"paper_mode": True},
        config={"paper_mode": False},
        engine_status="running",
    )
    assert _derive_mode(row) == "PAPER"


def test_mode_paper_when_only_engine_config_paper_mode_set():
    # Production shape: engine's heartbeat puts paper_mode under config.
    # Hub state may be empty (operator never toggled the hub endpoint).
    row = _row(
        state={},
        config={"paper_mode": True, "kill_switch_active": False},
        engine_status="running",
    )
    assert _derive_mode(row) == "PAPER"


def test_mode_paper_falls_back_to_paper_enabled_column():
    # Older engine builds set the dedicated column without filling the
    # jsonb. We honour that.
    row = _row(
        state={},
        config={},
        engine_status="running",
        paper_enabled=True,
    )
    assert _derive_mode(row) == "PAPER"


# ── LIVE precedence ─────────────────────────────────────────────────────────


def test_mode_live_against_real_engine_heartbeat_shape():
    # This is the exact shape engine/publish_heartbeat writes.
    row = _row(
        state={},
        config={
            "wallet_balance_usdc": 50.0,
            "daily_pnl": 0.0,
            "consecutive_losses": 0,
            "paper_mode": False,
            "kill_switch_active": False,
            "runtime_config": {},
        },
        engine_status="running",
        paper_enabled=False,
    )
    assert _derive_mode(row) == "LIVE"


def test_mode_live_accepts_legacy_active_string():
    row = _row(
        state={"status": "active"},
        config={},
        engine_status=None,
    )
    assert _derive_mode(row) == "LIVE"


def test_mode_live_accepts_engine_status_active():
    row = _row(state={}, config={}, engine_status="active")
    assert _derive_mode(row) == "LIVE"


# ── UNKNOWN ─────────────────────────────────────────────────────────────────


def test_mode_unknown_when_engine_status_is_starting_or_paused():
    # Operator-meaningful but not LIVE — collapse to UNKNOWN until we
    # add named modes for these. raw `engine_status` is also exposed on
    # the response payload so the FE tooltip can still surface detail.
    row = _row(state={}, config={}, engine_status="starting")
    assert _derive_mode(row) == "UNKNOWN"
    row = _row(state={}, config={}, engine_status="paused")
    assert _derive_mode(row) == "UNKNOWN"


def test_mode_unknown_when_engine_silent_and_no_paper_flag():
    row = _row(state={}, config={}, engine_status=None)
    assert _derive_mode(row) == "UNKNOWN"
