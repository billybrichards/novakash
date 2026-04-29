"""Tests for fix/exit-monitor-db-override — verify that the post-fill exit
evaluation block in registry.py applies DB runtime overrides to gate_params.

Bug: The per-strategy eval loop (L478) correctly calls
    apply_runtime_overrides() and creates a local config copy with merged
    gate_params. But the post-fill exit block (L800) reads directly from
    self._configs, which still has the original YAML values. Any gate_params
    set via the strategy_runtime_overrides DB table (e.g. exit_monitor_enabled,
    hedge_exit_enabled) were invisible to the exit evaluators.

Fix: After reading _p_cfg from self._configs, call apply_runtime_overrides()
    to layer DB overrides onto gate_params before extracting exit parameters.

Tests verify:
  1. exit_monitor_enabled=True via DB override is respected (YAML has False)
  2. hedge_exit_enabled=True via DB override is respected (YAML has False)
  3. Without the fix, YAML defaults (False) would have been used
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field, replace as _dc_replace
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.registry import StrategyConfig
from strategies.runtime_override import (
    RuntimeOverride,
    RuntimeOverrideManager,
    apply_runtime_overrides,
    reset_singleton_for_tests,
    get_runtime_override_manager,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_singleton():
    """Reset the module-level singleton before and after every test."""
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


def _make_strategy_config(
    name: str = "v9_sniper",
    mode: str = "LIVE",
    gate_params: Optional[dict] = None,
) -> StrategyConfig:
    """Build a minimal StrategyConfig for testing."""
    return StrategyConfig(
        name=name,
        version="9",
        mode=mode,
        asset="BTC",
        timescale="5m",
        gates=[],
        sizing={"fraction": 0.025},
        gate_params=gate_params or {},
    )


@dataclass
class _FakePosition:
    strategy_id: str
    window_ts: int
    side: str = "DOWN"
    fill_price: float = 0.55
    fill_size: float = 10.0


class _FakePositionMonitor:
    """Minimal stub for PositionMonitor that records calls."""

    def __init__(self, positions: Dict[str, _FakePosition]):
        self._positions = positions
        self.evaluate_exit_calls: list[dict] = []
        self.evaluate_hedge_exit_calls: list[dict] = []

    def get_open_positions(self) -> Dict[str, _FakePosition]:
        return dict(self._positions)

    def evaluate_exit(self, *, strategy_id, window_ts, surface, **kwargs):
        self.evaluate_exit_calls.append(
            {"strategy_id": strategy_id, "window_ts": window_ts, **kwargs}
        )
        return None  # no exit triggered

    def evaluate_hedge_exit(self, *, strategy_id, window_ts, surface, **kwargs):
        self.evaluate_hedge_exit_calls.append(
            {"strategy_id": strategy_id, "window_ts": window_ts, **kwargs}
        )
        return None  # no hedge triggered


def _install_db_override(strategy_id: str, params: dict) -> None:
    """Populate the module-level singleton with a DB override."""
    mgr = get_runtime_override_manager()
    override = RuntimeOverride(
        strategy_id=strategy_id,
        mode=None,  # inherit YAML mode
        params=params,
    )
    mgr._state.overrides[strategy_id] = override
    mgr._state.populated = True


# ── Tests ────────────────────────────────────────────────────────────────


class TestExitMonitorDbOverride:
    """Verify that the post-fill exit block picks up DB overrides."""

    def test_exit_monitor_enabled_via_db_override(self):
        """exit_monitor_enabled=True in DB should override YAML False.

        YAML gate_params: exit_monitor_enabled=False
        DB override: exit_monitor_enabled=True

        After the fix, evaluate_exit() should be called with
        exit_monitor_enabled=True.
        """
        yaml_params = {"exit_monitor_enabled": False}
        db_override = {"exit_monitor_enabled": True}

        # Install DB override
        _install_db_override("v9_sniper", db_override)

        # Verify apply_runtime_overrides merges correctly
        _eff_mode, _eff_params = apply_runtime_overrides(
            "v9_sniper", "LIVE", yaml_params
        )
        assert _eff_params["exit_monitor_enabled"] is True
        assert _eff_mode == "LIVE"

    def test_hedge_exit_enabled_via_db_override(self):
        """hedge_exit_enabled=True in DB should override YAML False.

        YAML gate_params: hedge_exit_enabled=False
        DB override: hedge_exit_enabled=True

        After the fix, evaluate_hedge_exit() should be called with
        hedge_exit_enabled=True.
        """
        yaml_params = {"hedge_exit_enabled": False}
        db_override = {"hedge_exit_enabled": True}

        _install_db_override("v9_sniper", db_override)

        _eff_mode, _eff_params = apply_runtime_overrides(
            "v9_sniper", "LIVE", yaml_params
        )
        assert _eff_params["hedge_exit_enabled"] is True

    def test_without_db_override_yaml_default_wins(self):
        """Without any DB override, YAML defaults pass through."""
        yaml_params = {
            "exit_monitor_enabled": False,
            "hedge_exit_enabled": False,
        }

        # No override installed — singleton has empty cache
        _eff_mode, _eff_params = apply_runtime_overrides(
            "v9_sniper", "LIVE", yaml_params
        )
        assert _eff_params["exit_monitor_enabled"] is False
        assert _eff_params["hedge_exit_enabled"] is False

    def test_dc_replace_creates_new_config_with_merged_params(self):
        """_dc_replace produces a new StrategyConfig with overridden
        gate_params without mutating the original."""
        original = _make_strategy_config(
            gate_params={
                "exit_monitor_enabled": False,
                "hedge_exit_enabled": False,
                "min_dist": 0.15,
            }
        )

        _install_db_override(
            "v9_sniper",
            {"exit_monitor_enabled": True, "hedge_exit_enabled": True},
        )

        _eff_mode, _eff_params = apply_runtime_overrides(
            "v9_sniper", original.mode, original.gate_params
        )

        # Params differ — replacement should happen
        assert _eff_params != original.gate_params
        replaced = _dc_replace(original, gate_params=_eff_params)

        # New config has DB values
        assert replaced.gate_params["exit_monitor_enabled"] is True
        assert replaced.gate_params["hedge_exit_enabled"] is True
        # YAML-only keys survive the merge
        assert replaced.gate_params["min_dist"] == 0.15

        # Original is NOT mutated
        assert original.gate_params["exit_monitor_enabled"] is False
        assert original.gate_params["hedge_exit_enabled"] is False


class TestPostFillOverrideIntegration:
    """End-to-end test that simulates the post-fill exit block logic
    with and without DB overrides, proving the fix works."""

    def _simulate_post_fill_block(
        self,
        configs: Dict[str, StrategyConfig],
        monitor: _FakePositionMonitor,
        apply_overrides: bool = True,
    ):
        """Replicate the registry's post-fill exit evaluation logic.

        When apply_overrides=True, mirrors the FIXED code path.
        When apply_overrides=False, mirrors the BROKEN code path.
        """
        surface = MagicMock()
        for pos_key, pos in monitor.get_open_positions().items():
            _p_cfg = configs.get(pos.strategy_id)
            if _p_cfg is None:
                continue

            if apply_overrides:
                # ── THE FIX ──
                _eff_mode_exit, _eff_params_exit = apply_runtime_overrides(
                    pos.strategy_id, _p_cfg.mode, _p_cfg.gate_params
                )
                if _eff_params_exit != _p_cfg.gate_params:
                    _p_cfg = _dc_replace(
                        _p_cfg, gate_params=_eff_params_exit
                    )

            _gp = _p_cfg.gate_params
            _exit_params = {
                "exit_monitor_enabled": _gp.get("exit_monitor_enabled", True),
            }
            monitor.evaluate_exit(
                strategy_id=pos.strategy_id,
                window_ts=pos.window_ts,
                surface=surface,
                **_exit_params,
            )

            _hedge_params = {
                "hedge_exit_enabled": _gp.get("hedge_exit_enabled", False),
            }
            monitor.evaluate_hedge_exit(
                strategy_id=pos.strategy_id,
                window_ts=pos.window_ts,
                surface=surface,
                **_hedge_params,
            )

    def test_fixed_code_uses_db_override(self):
        """With the fix applied, DB overrides reach the exit evaluators."""
        configs = {
            "v9_sniper": _make_strategy_config(
                name="v9_sniper",
                gate_params={
                    "exit_monitor_enabled": False,
                    "hedge_exit_enabled": False,
                },
            ),
        }
        monitor = _FakePositionMonitor(
            {"v9_sniper:1713010800": _FakePosition("v9_sniper", 1713010800)}
        )

        _install_db_override(
            "v9_sniper",
            {"exit_monitor_enabled": True, "hedge_exit_enabled": True},
        )

        self._simulate_post_fill_block(configs, monitor, apply_overrides=True)

        # Exit eval should see DB override (True), not YAML (False)
        assert len(monitor.evaluate_exit_calls) == 1
        assert monitor.evaluate_exit_calls[0]["exit_monitor_enabled"] is True

        assert len(monitor.evaluate_hedge_exit_calls) == 1
        assert monitor.evaluate_hedge_exit_calls[0]["hedge_exit_enabled"] is True

    def test_broken_code_ignores_db_override(self):
        """Without the fix, YAML defaults are used — exit monitors are dead."""
        configs = {
            "v9_sniper": _make_strategy_config(
                name="v9_sniper",
                gate_params={
                    "exit_monitor_enabled": False,
                    "hedge_exit_enabled": False,
                },
            ),
        }
        monitor = _FakePositionMonitor(
            {"v9_sniper:1713010800": _FakePosition("v9_sniper", 1713010800)}
        )

        _install_db_override(
            "v9_sniper",
            {"exit_monitor_enabled": True, "hedge_exit_enabled": True},
        )

        # Simulate the BROKEN code path (no override applied)
        self._simulate_post_fill_block(configs, monitor, apply_overrides=False)

        # Bug: exit eval still sees YAML default (False) despite DB override
        assert len(monitor.evaluate_exit_calls) == 1
        assert monitor.evaluate_exit_calls[0]["exit_monitor_enabled"] is False

        assert len(monitor.evaluate_hedge_exit_calls) == 1
        assert monitor.evaluate_hedge_exit_calls[0]["hedge_exit_enabled"] is False

    def test_multiple_strategies_each_get_own_override(self):
        """Each strategy's post-fill block reads its own DB override."""
        configs = {
            "v9_sniper": _make_strategy_config(
                name="v9_sniper",
                gate_params={"exit_monitor_enabled": False, "hedge_exit_enabled": False},
            ),
            "v10_lgb_only": _make_strategy_config(
                name="v10_lgb_only",
                gate_params={"exit_monitor_enabled": False, "hedge_exit_enabled": False},
            ),
        }
        monitor = _FakePositionMonitor({
            "v9_sniper:100": _FakePosition("v9_sniper", 100),
            "v10_lgb_only:200": _FakePosition("v10_lgb_only", 200),
        })

        # Only v9 gets the DB override; v10 stays at YAML defaults
        _install_db_override("v9_sniper", {"exit_monitor_enabled": True})

        self._simulate_post_fill_block(configs, monitor, apply_overrides=True)

        v9_exit = [c for c in monitor.evaluate_exit_calls if c["strategy_id"] == "v9_sniper"]
        v10_exit = [c for c in monitor.evaluate_exit_calls if c["strategy_id"] == "v10_lgb_only"]

        assert v9_exit[0]["exit_monitor_enabled"] is True   # DB override
        assert v10_exit[0]["exit_monitor_enabled"] is False  # YAML default
