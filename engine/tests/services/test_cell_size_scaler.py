"""Unit tests for engine/services/cell_size_scaler.py.

Covers:
* default 1.0 when no override / no provider / blank inputs
* reads override map correctly + UP/YES synonym handling
* clamps to cell_size_multiplier_max
* clamps to absolute_max_bet (hard cap)
* floors at MIN_BET_USD
* sub-1.0 entries treated as 1.0 (amp-only, never discount)
* malformed override map → 1.0 (graceful)
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from services.cell_size_scaler import (
    apply_cell_size_multiplier,
    cell_size_multiplier,
)
from services.session_label import session_for_hour, session_label


class _StubOverrideProvider:
    """Duck-typed minimal RuntimeOverrideManager for tests."""

    def __init__(self, params_by_strategy: Dict[str, Dict[str, Any]] | None = None):
        self._by = params_by_strategy or {}

    def get_effective_params(
        self, strategy_id: str, default: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self._by.get(strategy_id, dict(default))


# ─── cell_size_multiplier ────────────────────────────────────────────────


def test_default_when_no_provider():
    assert cell_size_multiplier("v12_lgb_combo", "us_open", "UP") == 1.0


def test_default_when_blank_args():
    p = _StubOverrideProvider({"v12_lgb_combo": {"cell_size_multipliers": {"us_open:UP": 1.5}}})
    assert cell_size_multiplier(None, "us_open", "UP", p) == 1.0
    assert cell_size_multiplier("v12_lgb_combo", "", "UP", p) == 1.0
    assert cell_size_multiplier("v12_lgb_combo", "us_open", None, p) == 1.0


def test_reads_override_map():
    p = _StubOverrideProvider(
        {"v12_lgb_combo": {"cell_size_multipliers": {"us_open:UP": 1.5}}}
    )
    assert cell_size_multiplier("v12_lgb_combo", "us_open", "UP", p) == 1.5


def test_yes_up_synonym_resolves_either_key():
    p_up = _StubOverrideProvider(
        {"s": {"cell_size_multipliers": {"us_open:UP": 1.3}}}
    )
    assert cell_size_multiplier("s", "us_open", "YES", p_up) == 1.3
    assert cell_size_multiplier("s", "us_open", "UP", p_up) == 1.3

    p_yes = _StubOverrideProvider(
        {"s": {"cell_size_multipliers": {"us_open:YES": 1.3}}}
    )
    assert cell_size_multiplier("s", "us_open", "YES", p_yes) == 1.3
    assert cell_size_multiplier("s", "us_open", "UP", p_yes) == 1.3

    p_no = _StubOverrideProvider({"s": {"cell_size_multipliers": {"us_late:NO": 1.25}}})
    assert cell_size_multiplier("s", "us_late", "DOWN", p_no) == 1.25


def test_clamps_to_cell_size_multiplier_max_default():
    """A 5.0 override is clamped to default cap 1.5."""
    p = _StubOverrideProvider(
        {"s": {"cell_size_multipliers": {"us_open:UP": 5.0}}}
    )
    assert cell_size_multiplier("s", "us_open", "UP", p) == 1.5


def test_clamps_to_explicit_cap():
    p = _StubOverrideProvider(
        {
            "s": {
                "cell_size_multipliers": {"us_open:UP": 5.0},
                "cell_size_multiplier_max": 2.0,
            }
        }
    )
    assert cell_size_multiplier("s", "us_open", "UP", p) == 2.0


def test_sub_one_entries_treated_as_one():
    """Multiplier is amp-only — a 0.5 entry must NOT shrink the stake."""
    p = _StubOverrideProvider({"s": {"cell_size_multipliers": {"us_open:UP": 0.5}}})
    assert cell_size_multiplier("s", "us_open", "UP", p) == 1.0


def test_missing_cell_returns_one():
    p = _StubOverrideProvider(
        {"s": {"cell_size_multipliers": {"us_open:UP": 1.5}}}
    )
    assert cell_size_multiplier("s", "asian_late", "UP", p) == 1.0
    assert cell_size_multiplier("s", "us_open", "DOWN", p) == 1.0


def test_malformed_override_map_returns_one():
    p = _StubOverrideProvider({"s": {"cell_size_multipliers": "not-a-dict"}})
    assert cell_size_multiplier("s", "us_open", "UP", p) == 1.0


def test_negative_cap_falls_back_to_default():
    p = _StubOverrideProvider(
        {
            "s": {
                "cell_size_multipliers": {"us_open:UP": 1.4},
                "cell_size_multiplier_max": -1.0,
            }
        }
    )
    # Default cap 1.5 used, so 1.4 passes through
    assert cell_size_multiplier("s", "us_open", "UP", p) == 1.4


def test_provider_exception_returns_one():
    class _Boom:
        def get_effective_params(self, *a, **kw):
            raise RuntimeError("db down")

    assert cell_size_multiplier("s", "us_open", "UP", _Boom()) == 1.0


# ─── apply_cell_size_multiplier ──────────────────────────────────────────


def test_apply_envelope_no_provider():
    env = apply_cell_size_multiplier(
        5.0,
        strategy_id="s",
        session="us_open",
        direction="UP",
        absolute_max_bet=20.0,
        min_bet_usd=2.0,
    )
    assert env["multiplier"] == 1.0
    assert env["final_stake"] == 5.0
    assert env["clamp_reason"] is None


def test_apply_envelope_amp_clamps_to_absolute_max_bet():
    """Multiplier 1.5 × $15 = $22.5 → clamp to $20 absolute max."""
    p = _StubOverrideProvider(
        {"s": {"cell_size_multipliers": {"us_open:UP": 1.5}}}
    )
    env = apply_cell_size_multiplier(
        15.0,
        strategy_id="s",
        session="us_open",
        direction="UP",
        absolute_max_bet=20.0,
        min_bet_usd=2.0,
        override_provider=p,
    )
    assert env["multiplier"] == 1.5
    assert env["scaled_stake"] == 22.5
    assert env["final_stake"] == 20.0
    assert env["clamp_reason"] == "absolute_max_bet"


def test_apply_envelope_floors_to_min_bet_usd():
    """Tiny base + 1.0 multiplier floors at MIN_BET_USD."""
    env = apply_cell_size_multiplier(
        0.5,
        strategy_id="s",
        session="us_open",
        direction="UP",
        absolute_max_bet=20.0,
        min_bet_usd=2.0,
    )
    assert env["final_stake"] == 2.0
    assert env["clamp_reason"] == "min_bet_floor"


def test_apply_envelope_normal_path_with_amp():
    """5.0 × 1.5 = 7.5, well under absolute cap."""
    p = _StubOverrideProvider(
        {"s": {"cell_size_multipliers": {"us_open:UP": 1.5}}}
    )
    env = apply_cell_size_multiplier(
        5.0,
        strategy_id="s",
        session="us_open",
        direction="UP",
        absolute_max_bet=20.0,
        min_bet_usd=2.0,
        override_provider=p,
    )
    assert env["multiplier"] == 1.5
    assert env["final_stake"] == 7.5
    assert env["clamp_reason"] is None


# ─── session_label ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hour, expected",
    [
        (0, "asian_early"),
        (1, "asian_early"),
        (2, "asian_late"),
        (6, "asian_late"),
        (7, "eu_open"),
        (11, "eu_open"),
        (12, "us_open"),
        (16, "us_open"),
        (17, "us_late"),
        (20, "us_late"),
        (21, "asian_early"),
        (23, "asian_early"),
    ],
)
def test_session_for_hour_boundaries(hour, expected):
    assert session_for_hour(hour) == expected


def test_session_for_hour_invalid_raises():
    with pytest.raises(ValueError):
        session_for_hour(-1)
    with pytest.raises(ValueError):
        session_for_hour(24)


def test_session_label_roundtrip():
    from datetime import datetime, timezone

    assert session_label(datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)) == "us_open"
    # Naive datetime is treated as UTC
    assert session_label(datetime(2026, 5, 6, 8, 0)) == "eu_open"
