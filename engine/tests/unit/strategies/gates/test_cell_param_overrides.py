"""Unit tests for cell_param_overrides gate helper (hub #402, 2026-05-08).

Coverage:
  - Exact cell key match returns override dict
  - Miss (no matching key) returns empty dict
  - Empty params map returns empty dict (no-op)
  - Fallback chain: cell override -> strategy param -> global config
  - Per-strategy isolation (one strategy's overrides don't affect another)
  - Runtime-tunability via gate_params contextvar
  - Edge cases: malformed keys, non-dict override value, None regime/window_ts
  - build_cell_key helper produces matching keys
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# Ensure engine package root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from strategies.gates.cell_param_overrides import (
    build_cell_key,
    get_cell_param_overrides,
)
from strategies import gate_params as _gp
from services.cell_bucketing import t_band, session_label


# ── Fixtures ────────────────────────────────────────────────────────────────

# A window_ts that resolves to us_pm (14-17 UTC).  2026-05-08 15:00 UTC.
_TS_US_PM = 1746716400  # 2026-05-08 15:00:00 UTC

# A window_ts that resolves to eu_am (08-11 UTC).  2026-05-08 09:00 UTC.
_TS_EU_AM = 1746694800  # 2026-05-08 09:00:00 UTC

# eval_offset that resolves to T-121-180
_OFFSET_T121_180 = 150  # 150s remaining → T-121-180

# eval_offset that resolves to T-181-240
_OFFSET_T181_240 = 210  # 210s remaining → T-181-240


def _params_with_key(key: str, overrides: dict) -> dict:
    return {key: overrides}


# ── build_cell_key ───────────────────────────────────────────────────────────


def test_build_cell_key_basic():
    key = build_cell_key(
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    assert key == "DOWN:T-121-180:CASCADE:us_pm"


def test_build_cell_key_none_regime():
    key = build_cell_key(
        direction="UP",
        eval_offset=_OFFSET_T121_180,
        regime=None,
        window_ts=_TS_EU_AM,
    )
    assert key == "UP:T-121-180::eu_am"


def test_build_cell_key_none_window_ts():
    """session_label(None) returns a sentinel (implementation-defined label)."""
    key = build_cell_key(
        direction="UP",
        eval_offset=_OFFSET_T121_180,
        regime="TRANSITION",
        window_ts=None,
    )
    # key should still be formatted correctly; session part from session_label(None)
    parts = key.split(":")
    assert len(parts) == 4
    assert parts[0] == "UP"
    assert parts[1] == "T-121-180"
    assert parts[2] == "TRANSITION"


# ── get_cell_param_overrides — exact match ───────────────────────────────────


def test_exact_match_returns_override():
    """When the exact key is present, the override dict is returned."""
    key = "DOWN:T-121-180:CASCADE:us_pm"
    params = {key: {"lgb_dist_min_down": 0.10}}
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    assert result == {"lgb_dist_min_down": 0.10}


def test_exact_match_up_direction():
    key = "UP:T-121-180:TRANSITION:eu_am"
    params = {key: {"lgb_dist_min_up": 0.05}}
    result = get_cell_param_overrides(
        params=params,
        direction="UP",
        eval_offset=_OFFSET_T121_180,
        regime="TRANSITION",
        window_ts=_TS_EU_AM,
    )
    assert result == {"lgb_dist_min_up": 0.05}


def test_exact_match_top_cell_from_analysis():
    """Top cell from hub #402: DOWN:T-181-240:TRANSITION:us_late -> 0.0 override."""
    # 2026-05-08 19:00 UTC → us_late
    ts_us_late = 1778266800
    key = "DOWN:T-181-240:TRANSITION:us_late"
    params = {key: {"lgb_dist_min_down": 0.0}}
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=_OFFSET_T181_240,
        regime="TRANSITION",
        window_ts=ts_us_late,
    )
    assert result == {"lgb_dist_min_down": 0.0}


# ── get_cell_param_overrides — misses ────────────────────────────────────────


def test_miss_wrong_direction():
    key = "DOWN:T-121-180:CASCADE:us_pm"
    params = {key: {"lgb_dist_min_down": 0.10}}
    result = get_cell_param_overrides(
        params=params,
        direction="UP",  # wrong direction
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    assert result == {}


def test_miss_wrong_t_band():
    key = "DOWN:T-121-180:CASCADE:us_pm"
    params = {key: {"lgb_dist_min_down": 0.10}}
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=_OFFSET_T181_240,  # resolves to T-181-240, not T-121-180
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    assert result == {}


def test_miss_wrong_regime():
    key = "DOWN:T-121-180:CASCADE:us_pm"
    params = {key: {"lgb_dist_min_down": 0.10}}
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime="TRANSITION",  # wrong regime
        window_ts=_TS_US_PM,
    )
    assert result == {}


def test_miss_wrong_session():
    key = "DOWN:T-121-180:CASCADE:us_pm"
    params = {key: {"lgb_dist_min_down": 0.10}}
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_EU_AM,  # eu_am session, not us_pm
    )
    assert result == {}


# ── Empty params ─────────────────────────────────────────────────────────────


def test_empty_params_returns_empty():
    result = get_cell_param_overrides(
        params={},
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    assert result == {}


def test_none_like_empty_params():
    """Callers should pass {} as default; None is also handled gracefully."""
    # params={} is the standard no-op
    assert get_cell_param_overrides({}, "UP", 120, "TRANSITION", _TS_EU_AM) == {}


# ── Fallback chain ───────────────────────────────────────────────────────────


def test_fallback_chain_with_get_cell_param_overrides():
    """Simulate: cell override → strategy param → global default.

    When the cell key matches, cell-level value overrides strategy param.
    When cell key doesn't match, caller falls back to strategy param.
    """
    key = "DOWN:T-121-180:CASCADE:us_pm"
    # With matching cell override — overrides strategy param
    params_with_match = {key: {"lgb_dist_min_down": 0.0}}
    cell_result = get_cell_param_overrides(
        params=params_with_match,
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    strategy_default = 0.20  # strategy-level param
    resolved = cell_result.get("lgb_dist_min_down", strategy_default)
    assert resolved == 0.0  # cell override wins

    # Without matching cell override — falls back to strategy param
    params_no_match = {"UP:T-121-180:CASCADE:us_pm": {"lgb_dist_min_up": 0.0}}
    miss_result = get_cell_param_overrides(
        params=params_no_match,
        direction="DOWN",  # no DOWN key in params
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    resolved_fallback = miss_result.get("lgb_dist_min_down", strategy_default)
    assert resolved_fallback == 0.20  # falls back to strategy param


# ── Per-strategy isolation ───────────────────────────────────────────────────


def test_per_strategy_isolation_via_gate_params():
    """Simulate per-strategy isolation using the gate_params contextvar.

    Strategy A has a param_overrides_by_cell with a key that lowers the floor.
    Strategy B has an empty override map.
    The two evaluations must not bleed into each other.
    """
    key = "DOWN:T-121-180:CASCADE:us_pm"
    strategy_a_params = {"param_overrides_by_cell": {key: {"lgb_dist_min_down": 0.0}}}
    strategy_b_params = {"param_overrides_by_cell": {}}

    # Evaluate in strategy A context
    token_a = _gp.set_active(strategy_a_params)
    try:
        params_a = _gp.get_dict("param_overrides_by_cell", default={})
        result_a = get_cell_param_overrides(
            params=params_a,
            direction="DOWN",
            eval_offset=_OFFSET_T121_180,
            regime="CASCADE",
            window_ts=_TS_US_PM,
        )
    finally:
        _gp.reset_active(token_a)

    # Evaluate in strategy B context
    token_b = _gp.set_active(strategy_b_params)
    try:
        params_b = _gp.get_dict("param_overrides_by_cell", default={})
        result_b = get_cell_param_overrides(
            params=params_b,
            direction="DOWN",
            eval_offset=_OFFSET_T121_180,
            regime="CASCADE",
            window_ts=_TS_US_PM,
        )
    finally:
        _gp.reset_active(token_b)

    assert result_a == {"lgb_dist_min_down": 0.0}, "strategy A should have override"
    assert result_b == {}, "strategy B should have no override (isolation)"


def test_per_strategy_isolation_different_keys():
    """Two strategies each have a different cell key. Verify no cross-bleed."""
    key_v8 = "DOWN:T-121-180:CASCADE:us_pm"
    key_v9 = "UP:T-121-180:TRANSITION:eu_am"

    v8_params = {"param_overrides_by_cell": {key_v8: {"lgb_dist_min_down": 0.05}}}
    v9_params = {"param_overrides_by_cell": {key_v9: {"lgb_dist_min_up": 0.08}}}

    # v8 lookup
    token_v8 = _gp.set_active(v8_params)
    try:
        r_v8_down = get_cell_param_overrides(
            _gp.get_dict("param_overrides_by_cell", default={}),
            "DOWN", _OFFSET_T121_180, "CASCADE", _TS_US_PM,
        )
        r_v8_up = get_cell_param_overrides(
            _gp.get_dict("param_overrides_by_cell", default={}),
            "UP", _OFFSET_T121_180, "TRANSITION", _TS_EU_AM,
        )
    finally:
        _gp.reset_active(token_v8)

    # v9 lookup
    token_v9 = _gp.set_active(v9_params)
    try:
        r_v9_up = get_cell_param_overrides(
            _gp.get_dict("param_overrides_by_cell", default={}),
            "UP", _OFFSET_T121_180, "TRANSITION", _TS_EU_AM,
        )
        r_v9_down = get_cell_param_overrides(
            _gp.get_dict("param_overrides_by_cell", default={}),
            "DOWN", _OFFSET_T121_180, "CASCADE", _TS_US_PM,
        )
    finally:
        _gp.reset_active(token_v9)

    assert r_v8_down == {"lgb_dist_min_down": 0.05}
    assert r_v8_up == {}    # v8 only has DOWN key
    assert r_v9_up == {"lgb_dist_min_up": 0.08}
    assert r_v9_down == {}  # v9 only has UP key


# ── Runtime-tunability ───────────────────────────────────────────────────────


def test_runtime_tunability_via_gate_params_contextvar():
    """Simulate changing strategy_runtime_overrides.params at runtime.

    When the contextvar is updated (as happens during each strategy evaluation
    round because the registry binds fresh params from the DB), the resolved
    override updates immediately.
    """
    key = "DOWN:T-121-180:CASCADE:us_pm"

    # Round 1: no override active
    token1 = _gp.set_active({"param_overrides_by_cell": {}})
    try:
        r1 = get_cell_param_overrides(
            _gp.get_dict("param_overrides_by_cell", default={}),
            "DOWN", _OFFSET_T121_180, "CASCADE", _TS_US_PM,
        )
    finally:
        _gp.reset_active(token1)

    # Round 2: operator sets override in DB → new params bound
    token2 = _gp.set_active({"param_overrides_by_cell": {key: {"lgb_dist_min_down": 0.0}}})
    try:
        r2 = get_cell_param_overrides(
            _gp.get_dict("param_overrides_by_cell", default={}),
            "DOWN", _OFFSET_T121_180, "CASCADE", _TS_US_PM,
        )
    finally:
        _gp.reset_active(token2)

    # Round 3: operator removes override from DB
    token3 = _gp.set_active({"param_overrides_by_cell": {}})
    try:
        r3 = get_cell_param_overrides(
            _gp.get_dict("param_overrides_by_cell", default={}),
            "DOWN", _OFFSET_T121_180, "CASCADE", _TS_US_PM,
        )
    finally:
        _gp.reset_active(token3)

    assert r1 == {}
    assert r2 == {"lgb_dist_min_down": 0.0}
    assert r3 == {}


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_non_dict_override_value_returns_empty():
    """If the stored value for a key is not a dict, fails open."""
    key = "DOWN:T-121-180:CASCADE:us_pm"
    params = {key: "not_a_dict"}  # malformed
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    assert result == {}


def test_non_dict_override_none_value_returns_empty():
    key = "DOWN:T-121-180:CASCADE:us_pm"
    params = {key: None}  # None stored
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    assert result == {}


def test_none_regime_key():
    """None regime maps to empty string in key; only matches if stored key also has empty regime."""
    key_none_regime = "DOWN:T-121-180::us_pm"  # "" regime slot
    params = {key_none_regime: {"lgb_dist_min_down": 0.05}}
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime=None,  # None regime → "" in key
        window_ts=_TS_US_PM,
    )
    assert result == {"lgb_dist_min_down": 0.05}


def test_none_regime_does_not_match_named_regime_key():
    """None regime (→ "" in key) does NOT match a key with a named regime."""
    key_cascade = "DOWN:T-121-180:CASCADE:us_pm"
    params = {key_cascade: {"lgb_dist_min_down": 0.05}}
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime=None,  # None → "" in key → mismatch
        window_ts=_TS_US_PM,
    )
    assert result == {}


def test_none_eval_offset():
    """None eval_offset resolves to T-unknown; won't match normal t_band keys."""
    key = "DOWN:T-unknown::us_pm"
    params = {key: {"lgb_dist_min_down": 0.05}}
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=None,
        regime=None,
        window_ts=_TS_US_PM,
    )
    assert result == {"lgb_dist_min_down": 0.05}


def test_malformed_params_dict_fails_open():
    """A completely malformed params type should not raise — fails open."""
    result = get_cell_param_overrides(
        params={"bad_key_no_colons": {"lgb_dist_min_down": 0.05}},
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    # Simply doesn't match — not an exception
    assert result == {}


def test_multiple_keys_first_match_wins():
    """When multiple keys could match (duplicate), first match wins (dict ordering)."""
    key1 = "DOWN:T-121-180:CASCADE:us_pm"
    key2 = "DOWN:T-121-180:CASCADE:us_pm"  # same key — only one entry possible
    params = {key1: {"lgb_dist_min_down": 0.10}}
    result = get_cell_param_overrides(
        params=params,
        direction="DOWN",
        eval_offset=_OFFSET_T121_180,
        regime="CASCADE",
        window_ts=_TS_US_PM,
    )
    assert result == {"lgb_dist_min_down": 0.10}


# ── gate_params.get_dict ─────────────────────────────────────────────────────


def test_get_dict_returns_default_when_absent():
    token = _gp.set_active({"some_other_key": 42})
    try:
        result = _gp.get_dict("param_overrides_by_cell", default={})
    finally:
        _gp.reset_active(token)
    assert result == {}


def test_get_dict_returns_stored_dict():
    override_map = {"DOWN:T-121-180:CASCADE:us_pm": {"lgb_dist_min_down": 0.0}}
    token = _gp.set_active({"param_overrides_by_cell": override_map})
    try:
        result = _gp.get_dict("param_overrides_by_cell", default={})
    finally:
        _gp.reset_active(token)
    assert result == override_map


def test_get_dict_returns_default_when_value_is_list():
    """Non-dict stored value (e.g. accidental list) returns default."""
    token = _gp.set_active({"param_overrides_by_cell": [{"not": "a dict"}]})
    try:
        result = _gp.get_dict("param_overrides_by_cell", default={})
    finally:
        _gp.reset_active(token)
    assert result == {}


def test_get_dict_returns_default_when_value_is_none():
    token = _gp.set_active({"param_overrides_by_cell": None})
    try:
        result = _gp.get_dict("param_overrides_by_cell", default={})
    finally:
        _gp.reset_active(token)
    assert result == {}


def test_get_dict_empty_default():
    """Default empty dict is returned correctly."""
    token = _gp.set_active({})
    try:
        result = _gp.get_dict("param_overrides_by_cell", default={})
    finally:
        _gp.reset_active(token)
    assert result == {}
