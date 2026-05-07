"""Unit tests for engine/services/cell_bucketing.py.

Pins every hour boundary for both t_band and session_label so that any
accidental drift is caught at review time. Boundaries must match the
Hub note #350 SQL CASE expression (reproduced in cell_bucketing.session_label
docstring for cross-reference).

session_label 7-bucket vocabulary (canonical):
    asian_early: 0-3  UTC
    asian_late:  4-7  UTC
    eu_am:       8-11 UTC
    us_open:    12-13 UTC
    us_pm:      14-17 UTC
    us_late:    18-21 UTC
    off_hours:  22-23 UTC
"""
from __future__ import annotations

import pytest

from services.cell_bucketing import session_label, session, t_band


# ── session_label: all 24 hours ───────────────────────────────────────────


@pytest.mark.parametrize("hour,expected", [
    (0, "asian_early"),
    (1, "asian_early"),
    (2, "asian_early"),
    (3, "asian_early"),
    (4, "asian_late"),
    (5, "asian_late"),
    (6, "asian_late"),
    (7, "asian_late"),
    (8, "eu_am"),
    (9, "eu_am"),
    (10, "eu_am"),
    (11, "eu_am"),
    (12, "us_open"),
    (13, "us_open"),
    (14, "us_pm"),
    (15, "us_pm"),
    (16, "us_pm"),
    (17, "us_pm"),
    (18, "us_late"),
    (19, "us_late"),
    (20, "us_late"),
    (21, "us_late"),
    (22, "off_hours"),
    (23, "off_hours"),
])
def test_session_label_all_hours(hour, expected):
    assert session_label(hour) == expected


def test_session_label_none_returns_unknown():
    assert session_label(None) == "unknown"


def test_session_alias_equals_session_label():
    """session() is a backward-compat alias for session_label()."""
    for h in range(24):
        assert session(h) == session_label(h)


# ── session_label boundary precision ─────────────────────────────────────


def test_asian_early_to_asian_late_boundary():
    """Hour 3 = asian_early, hour 4 = asian_late."""
    assert session_label(3) == "asian_early"
    assert session_label(4) == "asian_late"


def test_asian_late_to_eu_am_boundary():
    """Hour 7 = asian_late, hour 8 = eu_am."""
    assert session_label(7) == "asian_late"
    assert session_label(8) == "eu_am"


def test_eu_am_to_us_open_boundary():
    """Hour 11 = eu_am, hour 12 = us_open."""
    assert session_label(11) == "eu_am"
    assert session_label(12) == "us_open"


def test_us_open_to_us_pm_boundary():
    """Hour 13 = us_open, hour 14 = us_pm.
    NOTE: us_open is only 2 hours (12-13 UTC) per note #350 — narrower
    than the old 5-bucket scheme's 12-17 UTC 'us_open'."""
    assert session_label(13) == "us_open"
    assert session_label(14) == "us_pm"


def test_us_pm_to_us_late_boundary():
    """Hour 17 = us_pm, hour 18 = us_late."""
    assert session_label(17) == "us_pm"
    assert session_label(18) == "us_late"


def test_us_late_to_off_hours_boundary():
    """Hour 21 = us_late, hour 22 = off_hours."""
    assert session_label(21) == "us_late"
    assert session_label(22) == "off_hours"


# ── t_band: representative values ────────────────────────────────────────


@pytest.mark.parametrize("eval_offset,expected", [
    (0, "T-0-30"),
    (30, "T-0-30"),
    (31, "T-31-60"),
    (60, "T-31-60"),
    (61, "T-61-90"),
    (90, "T-61-90"),
    (91, "T-91-120"),
    (120, "T-91-120"),
    (121, "T-121-180"),
    (180, "T-121-180"),
    (181, "T-181-240"),
    (240, "T-181-240"),
    (241, "T-241-300"),
    (300, "T-241-300"),
    (88, "T-61-90"),   # v12_lgb_combo typical fire offset
    (102, "T-91-120"),
])
def test_t_band_boundaries(eval_offset, expected):
    assert t_band(eval_offset) == expected


def test_t_band_none_returns_unknown():
    assert t_band(None) == "T-unknown"
