"""
Hub /api/system/status mode-derivation tests.

The endpoint adds a derived `mode` field (LIVE / PAPER / KILLED / UNKNOWN)
the FE renders directly. These tests cover every precedence branch in
api.system._derive_mode without booting an HTTP client — _derive_mode is
deliberately written as a pure function so the test stays simple.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.system import _derive_mode


def _row(state, paper_enabled=False):
    return SimpleNamespace(state=state, paper_enabled=paper_enabled)


def test_mode_unknown_when_no_state_row():
    assert _derive_mode(None) == "UNKNOWN"


def test_mode_killed_takes_precedence_over_paper_and_active():
    row = _row(
        {"kill_switch_manual": True, "paper_mode": True, "status": "active"},
    )
    assert _derive_mode(row) == "KILLED"


def test_mode_killed_via_auto_drawdown_kill():
    row = _row({"kill_switch_auto": True, "status": "active"})
    assert _derive_mode(row) == "KILLED"


def test_mode_paper_when_paper_mode_set_and_not_killed():
    row = _row({"paper_mode": True, "status": "active"})
    assert _derive_mode(row) == "PAPER"


def test_mode_paper_falls_back_to_paper_enabled_column():
    # Older engine builds set the dedicated column without filling the jsonb.
    row = _row({"status": "active"}, paper_enabled=True)
    assert _derive_mode(row) == "PAPER"


def test_mode_live_when_active_and_not_paper():
    row = _row({"status": "active", "paper_mode": False})
    assert _derive_mode(row) == "LIVE"


def test_mode_unknown_when_status_not_active():
    row = _row({"status": "starting"})
    assert _derive_mode(row) == "UNKNOWN"


def test_mode_unknown_when_state_blob_empty():
    row = _row({})
    assert _derive_mode(row) == "UNKNOWN"
