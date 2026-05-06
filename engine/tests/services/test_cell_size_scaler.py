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
    """A 5.0 override is clamped to default cap 2.0 (bumped from 1.5 for sniper cells)."""
    p = _StubOverrideProvider(
        {"s": {"cell_size_multipliers": {"us_open:UP": 5.0}}}
    )
    assert cell_size_multiplier("s", "us_open", "UP", p) == 2.0


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


# ─── Multi-axis lookup priority tests ────────────────────────────────────


def _make_provider(strategy_id: str, overrides: dict) -> _StubOverrideProvider:
    """Convenience builder."""
    return _StubOverrideProvider({strategy_id: {"cell_size_multipliers": overrides}})


# ── Layer 1: most-specific wins when multiple keys match ─────────────────

def test_most_specific_4axis_wins():
    """session:dir:tband:regime wins over less specific keys when present."""
    p = _make_provider("s", {
        "asian_late:DOWN:T-121-180:CASCADE": 2.0,
        "asian_late:DOWN:T-121-180": 1.5,
        "asian_late:DOWN": 1.25,
    })
    # eval_offset=140 → T-121-180; regime=CASCADE
    result = cell_size_multiplier("s", "asian_late", "DOWN", p, t_band=140, regime="CASCADE")
    assert result == 2.0


def test_fallthrough_to_3axis_session_dir_tband():
    """Falls through to session:dir:tband when 4-axis key absent."""
    p = _make_provider("s", {
        "asian_late:DOWN:T-121-180": 1.5,
        "asian_late:DOWN": 1.25,
    })
    result = cell_size_multiplier("s", "asian_late", "DOWN", p, t_band=140, regime="CASCADE")
    assert result == 1.5


def test_fallthrough_to_3axis_session_dir_regime():
    """Falls through to session:dir:regime when session:dir:tband absent."""
    p = _make_provider("s", {
        "asian_late:DOWN:CASCADE": 1.3,
        "asian_late:DOWN": 1.25,
    })
    result = cell_size_multiplier("s", "asian_late", "DOWN", p, t_band=140, regime="CASCADE")
    assert result == 1.3


def test_fallthrough_to_2axis_session_dir():
    """Falls through to session:dir (original behaviour) when richer keys absent."""
    p = _make_provider("s", {
        "asian_late:DOWN": 1.25,
    })
    result = cell_size_multiplier("s", "asian_late", "DOWN", p, t_band=140, regime="CASCADE")
    assert result == 1.25


def test_fallthrough_to_dir_tband_regime():
    """Falls through to dir:tband:regime when session-specific keys absent."""
    p = _make_provider("s", {
        "DOWN:T-121-180:CASCADE": 1.4,
    })
    result = cell_size_multiplier("s", "asian_late", "DOWN", p, t_band=140, regime="CASCADE")
    assert result == 1.4


def test_fallthrough_to_tband_regime():
    """Falls through to tband:regime (no session, no dir) as last resort before 1.0."""
    p = _make_provider("s", {
        "T-121-180:CASCADE": 1.2,
    })
    result = cell_size_multiplier("s", "asian_late", "DOWN", p, t_band=140, regime="CASCADE")
    assert result == 1.2


def test_fallthrough_all_the_way_to_default_one():
    """Returns 1.0 when no key in chain matches."""
    p = _make_provider("s", {
        "us_open:UP:T-0-30:NORMAL": 1.5,  # completely different cell
    })
    result = cell_size_multiplier("s", "asian_late", "DOWN", p, t_band=140, regime="CASCADE")
    assert result == 1.0


def test_default_when_override_empty():
    """Empty override map → 1.0."""
    p = _make_provider("s", {})
    assert cell_size_multiplier("s", "asian_late", "DOWN", p, t_band=140, regime="CASCADE") == 1.0


def test_default_when_override_none():
    """No override provider → 1.0."""
    assert cell_size_multiplier("s", "asian_late", "DOWN", None, t_band=140, regime="CASCADE") == 1.0


# ── Real-cell scenarios ──────────────────────────────────────────────────

def test_sniper_cell_asian_late_down_cascade():
    """asian_late:DOWN:T-121-180:CASCADE → 2.0 overrides less-specific keys."""
    p = _make_provider("v12_lgb_combo", {
        "asian_late:DOWN:T-121-180:CASCADE": 2.0,
        "asian_late:DOWN:T-121-180": 1.5,
        "asian_late:DOWN": 1.25,
    })
    # eval_offset=140 → T-121-180
    result = cell_size_multiplier("v12_lgb_combo", "asian_late", "DOWN", p, t_band=140, regime="CASCADE")
    assert result == 2.0


def test_workhorse_cell_fires_without_regime_key():
    """eu_pm_us_am:UP:T-91-120 → 1.5 when eu_pm_us_am:UP:T-91-120:NORMAL not set."""
    p = _make_provider("v12_lgb_combo", {
        "eu_pm_us_am:UP:T-91-120": 1.5,
        "eu_pm_us_am:UP": 1.2,
    })
    # eval_offset=100 → T-91-120; regime=NORMAL (4-axis key absent)
    result = cell_size_multiplier("v12_lgb_combo", "eu_pm_us_am", "UP", p, t_band=100, regime="NORMAL")
    assert result == 1.5


def test_strategy_wide_down_bump_as_last_fallback():
    """DOWN direction-only key fires as last fallback before 1.0 is not in current chain.

    Note: a bare "DOWN" key is NOT currently in the lookup chain (lowest priority
    is tband:regime). This test documents that bare direction-only keys must be
    set at a layer that IS in the chain — e.g. set as session:dir or dir:tband:regime.
    Using it via session:dir requires session to be non-None; if the override is
    set as asian_late:DOWN it acts as the session:dir fallback.
    """
    p = _make_provider("v12_lgb_combo", {
        "asian_late:DOWN": 1.25,
    })
    # Only session:dir key set — fires correctly as layer 4
    result = cell_size_multiplier("v12_lgb_combo", "asian_late", "DOWN", p, t_band=140, regime="CASCADE")
    assert result == 1.25


# ── cell_size_multiplier_max cap ─────────────────────────────────────────

def test_cap_clamps_sniper_override():
    """Override 3.0 with cell_size_multiplier_max 2.0 → returns 2.0."""
    p = _StubOverrideProvider({
        "v12_lgb_combo": {
            "cell_size_multipliers": {"asian_late:DOWN:T-121-180:CASCADE": 3.0},
            "cell_size_multiplier_max": 2.0,
        }
    })
    result = cell_size_multiplier("v12_lgb_combo", "asian_late", "DOWN", p, t_band=140, regime="CASCADE")
    assert result == 2.0


# ── Backward-compatibility tests ─────────────────────────────────────────

def test_existing_session_dir_keys_still_work():
    """Pre-multi-axis session:dir keys resolve correctly without t_band/regime."""
    p = _make_provider("v12_lgb_combo", {
        "us_open:UP": 1.5,
        "asian_late:UP": 1.25,
        "us_late:DOWN": 1.25,
    })
    assert cell_size_multiplier("v12_lgb_combo", "us_open", "UP", p) == 1.5
    assert cell_size_multiplier("v12_lgb_combo", "asian_late", "UP", p) == 1.25
    assert cell_size_multiplier("v12_lgb_combo", "us_late", "DOWN", p) == 1.25


def test_calling_without_t_band_regime_args_resolves():
    """Backward compat: calling with only (strategy_id, session, direction) still works."""
    p = _make_provider("v12_lgb_combo", {
        "us_open:UP": 1.5,
    })
    # No t_band or regime kwargs
    assert cell_size_multiplier("v12_lgb_combo", "us_open", "UP", p) == 1.5


def test_yes_synonym_with_t_band():
    """YES synonym resolves to the UP-keyed entry in a 4-axis key."""
    p = _make_provider("s", {
        "asian_late:UP:T-121-180:CASCADE": 1.8,
    })
    result = cell_size_multiplier("s", "asian_late", "YES", p, t_band=140, regime="CASCADE")
    assert result == 1.8


# ── Integration: _calculate_stake t_band + regime wiring ─────────────────

class _StubDecisionWithMeta:
    """Minimal StrategyDecision-like object for execute_trade._calculate_stake tests."""

    def __init__(self, strategy_id, direction, eval_offset=None, vpin_regime=None, entry_cap=0.50):
        self.strategy_id = strategy_id
        self.direction = direction
        self.entry_cap = entry_cap
        self.metadata = {}
        if eval_offset is not None:
            self.metadata["eval_offset"] = eval_offset
        if vpin_regime is not None:
            self.metadata["vpin_regime"] = vpin_regime


def test_apply_envelope_with_t_band_and_regime():
    """eval_offset=140 + CASCADE + override key → final stake clamped at abs max."""
    p = _make_provider("v12_lgb_combo", {
        "asian_late:DOWN:T-121-180:CASCADE": 2.0,
    })
    env = apply_cell_size_multiplier(
        20.0,
        strategy_id="v12_lgb_combo",
        session="asian_late",
        direction="DOWN",
        absolute_max_bet=30.0,
        min_bet_usd=2.0,
        override_provider=p,
        t_band=140,
        regime="CASCADE",
    )
    assert env["multiplier"] == 2.0
    assert env["scaled_stake"] == 40.0
    assert env["final_stake"] == 30.0  # clamped
    assert env["clamp_reason"] == "absolute_max_bet"
    assert env["t_band"] == "T-121-180"
    assert env["regime"] == "CASCADE"


def test_apply_envelope_empty_override_returns_base():
    """Empty override → multiplier 1.0 → final stake unchanged."""
    p = _make_provider("v12_lgb_combo", {})
    env = apply_cell_size_multiplier(
        20.0,
        strategy_id="v12_lgb_combo",
        session="asian_late",
        direction="DOWN",
        absolute_max_bet=30.0,
        min_bet_usd=2.0,
        override_provider=p,
        t_band=140,
        regime="CASCADE",
    )
    assert env["multiplier"] == 1.0
    assert env["final_stake"] == 20.0


def test_apply_envelope_missing_eval_offset_uses_session_dir():
    """When t_band=None, scaler falls through to session:dir layer."""
    p = _make_provider("v12_lgb_combo", {
        "asian_late:DOWN": 1.25,
    })
    # t_band=None, regime=None → only session:dir available
    env = apply_cell_size_multiplier(
        20.0,
        strategy_id="v12_lgb_combo",
        session="asian_late",
        direction="DOWN",
        absolute_max_bet=30.0,
        min_bet_usd=2.0,
        override_provider=p,
        t_band=None,
        regime=None,
    )
    assert env["multiplier"] == 1.25
    assert env["final_stake"] == 25.0
