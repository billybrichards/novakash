"""Tests for ``StrategyRegistry._effective_configs`` (TG card visibility fix).

Bug being regression-guarded
----------------------------
2026-04-27: v10_lgb_only was promoted GHOST→LIVE via the
``strategy_runtime_overrides`` DB table. Strategy decisions DID flow through
the LIVE path, but TG window-summary cards never showed v10 lines. Root cause:
``_send_window_summary`` (and the per-strategy skip-card emitter) read
``self._configs`` (raw YAML) — so v10 was bucketed under GHOST and collapsed
into the shadow line, even when the runtime override had flipped it LIVE.

The fix introduces ``_effective_configs()`` which applies runtime overrides
before returning the dict. These tests pin that behaviour.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..")
)

from strategies.registry import StrategyConfig, StrategyRegistry  # noqa: E402
from strategies.runtime_override import (  # noqa: E402
    RuntimeOverride,
    RuntimeOverrideManager,
    reset_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


def _bare_registry() -> StrategyRegistry:
    """Skip the heavy YAML-loading __init__ — we only need _effective_configs.

    Allocating a __new__'d instance and stuffing _configs is enough to test
    the helper in isolation.
    """
    reg = StrategyRegistry.__new__(StrategyRegistry)
    reg._configs = {}
    return reg


def _stub_config(name: str, mode: str, gate_params: Optional[Dict[str, Any]] = None) -> StrategyConfig:
    return StrategyConfig(
        name=name,
        version="test",
        mode=mode,
        asset="BTC",
        timescale="5m",
        gates=[],
        gate_params=gate_params or {},
        sizing={},
        hooks_file=None,
    )


def _seed_override(strategy_id: str, mode: Optional[str], params: Optional[Dict[str, Any]] = None):
    """Force a runtime override into the singleton manager — bypasses DB."""
    from strategies import runtime_override as _ro

    mgr = _ro.get_runtime_override_manager()
    overrides = {
        strategy_id: RuntimeOverride(strategy_id=strategy_id, mode=mode, params=params)
    }
    mgr._apply_rows(overrides)
    return mgr


def test_no_override_returns_yaml_configs_unchanged():
    reg = _bare_registry()
    reg._configs["v9_lgb_only"] = _stub_config("v9_lgb_only", "LIVE")
    reg._configs["v10_lgb_only"] = _stub_config("v10_lgb_only", "GHOST")

    eff = reg._effective_configs()
    assert eff["v9_lgb_only"].mode == "LIVE"
    assert eff["v10_lgb_only"].mode == "GHOST"


def test_runtime_override_promotes_ghost_to_live():
    """The exact bug — v10 yaml says GHOST, override says LIVE; the
    summary helper must see LIVE so v10 lines render in the TG card.
    """
    reg = _bare_registry()
    reg._configs["v10_lgb_only"] = _stub_config("v10_lgb_only", "GHOST")
    _seed_override("v10_lgb_only", mode="LIVE")

    eff = reg._effective_configs()
    assert eff["v10_lgb_only"].mode == "LIVE", (
        "Effective mode must reflect runtime override — otherwise window "
        "summary buckets v10 under GHOST and the operator never sees it."
    )


def test_runtime_override_demotes_live_to_ghost():
    reg = _bare_registry()
    reg._configs["v9_lgb_only"] = _stub_config("v9_lgb_only", "LIVE")
    _seed_override("v9_lgb_only", mode="GHOST")
    eff = reg._effective_configs()
    assert eff["v9_lgb_only"].mode == "GHOST"


def test_runtime_override_merges_gate_params():
    reg = _bare_registry()
    reg._configs["v9_lgb_only"] = _stub_config(
        "v9_lgb_only",
        "LIVE",
        gate_params={"post_loss_cooldown_min": 30, "vpin_min": 0.4},
    )
    _seed_override("v9_lgb_only", mode=None, params={"post_loss_cooldown_min": 0})

    eff = reg._effective_configs()
    assert eff["v9_lgb_only"].mode == "LIVE"
    # DB param wins on conflict; YAML keys absent from DB survive.
    assert eff["v9_lgb_only"].gate_params["post_loss_cooldown_min"] == 0
    assert eff["v9_lgb_only"].gate_params["vpin_min"] == 0.4


def test_unaffected_strategies_pass_through_object_identity():
    """Strategies with no override should not be reconstructed — keeps
    the helper cheap on the hot path."""
    reg = _bare_registry()
    cfg = _stub_config("v9_lgb_only", "LIVE")
    reg._configs["v9_lgb_only"] = cfg
    # Override targets a different strategy.
    _seed_override("v10_lgb_only", mode="LIVE")

    eff = reg._effective_configs()
    # Same Python object — no replace() round-trip when nothing changed.
    assert eff["v9_lgb_only"] is cfg
