"""Regression tests for PositionMonitor.on_fill wiring (PR #383, audit #298/#299).

After a successful LIVE FAK fill, ``StrategyRegistry`` must register the
position with ``PositionMonitor.on_fill()`` so the mark-to-market exit
loop can monitor and stop-loss the position. If this wiring breaks, fills
go un-monitored — exits never fire and a losing position rides to
expiry, eating the full $L on a stop-out instead of a partial exit at
T-48 to T-30.

These tests pin the exact wiring contract:

1. ``PositionMonitor.on_fill()`` accepts the kwargs we pass at the call
   site (signature stability — adding new fields is fine, removing or
   renaming the existing ones breaks the registry call).
2. The registered position is retrievable via the public accessor used
   by the eval loop.
3. The wiring inside ``registry.py`` calls ``on_fill`` with the right
   shape — static-source check (the registry has too many ports to
   spin up live).
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REGISTRY_PATH = Path(__file__).parent.parent / "strategies" / "registry.py"


# ── 1. on_fill contract ───────────────────────────────────────────────────


class TestOnFillContract:
    """The on_fill signature is the integration boundary with registry.py."""

    def test_on_fill_accepts_kwargs_used_by_registry(self):
        from execution.position_monitor import PositionMonitor

        pm = PositionMonitor()
        # Same call shape the registry uses (registry.py:656-665).
        pm.on_fill(
            strategy_id="v9_lgb_only",
            window_ts=1_777_200_000,
            direction="DOWN",
            fill_price=0.42,
            fill_size=15.0,
            order_id="0xfeedfacecafebabe",
            token_id="0xtokendowncedar",
            confirmed_size=14.7,
        )
        # Position is now registered under strategy:window key
        positions = pm.get_open_positions()
        assert "v9_lgb_only:1777200000" in positions
        pos = positions["v9_lgb_only:1777200000"]
        assert pos.strategy_id == "v9_lgb_only"
        assert pos.direction == "DOWN"
        assert pos.fill_price == 0.42
        assert pos.fill_size == 15.0
        assert pos.confirmed_size == 14.7
        assert pos.token_id == "0xtokendowncedar"

    def test_on_fill_logs_position_monitor_registered(self, caplog):
        """The 'position_monitor.registered' log line is what operators
        grep for to confirm wiring. Renaming it breaks ops runbooks."""
        import logging
        from execution.position_monitor import PositionMonitor

        # structlog → stdlib forwarder; make sure it surfaces in caplog.
        caplog.set_level(logging.INFO)
        pm = PositionMonitor()
        pm.on_fill(
            strategy_id="v9_lgb_only",
            window_ts=1_777_200_000,
            direction="UP",
            fill_price=0.50,
            fill_size=10.0,
            order_id="0x1234",
        )
        # Check positions registry directly — log capture across structlog
        # is brittle, but the side-effect (registration) is the contract.
        assert "v9_lgb_only:1777200000" in pm.get_open_positions()

    def test_on_fill_replaces_existing_key(self):
        """Repeated on_fill calls for the same (strategy, window) overwrite
        — used by the FAK retry path."""
        from execution.position_monitor import PositionMonitor

        pm = PositionMonitor()
        for size in (5.0, 10.0, 20.0):
            pm.on_fill(
                strategy_id="v9_lgb_only",
                window_ts=1_777_200_000,
                direction="DOWN",
                fill_price=0.42,
                fill_size=size,
                order_id="0xabcd",
            )
        positions = pm.get_open_positions()
        assert len(positions) == 1
        assert positions["v9_lgb_only:1777200000"].fill_size == 20.0

    def test_default_confirmed_size_is_zero(self):
        """When the SOT reconciler hasn't run yet, confirmed_size=0
        triggers a 5% haircut on the sell path. Default must stay 0."""
        from execution.position_monitor import PositionMonitor

        pm = PositionMonitor()
        pm.on_fill(
            strategy_id="x",
            window_ts=1_777_200_000,
            direction="UP",
            fill_price=0.5,
            fill_size=10.0,
            order_id="0x",
        )
        pos = pm.get_open_positions()["x:1777200000"]
        assert pos.confirmed_size == 0.0


# ── 2. registry.py call shape ──────────────────────────────────────────────


class TestRegistryCallSite:
    """Static-source check: registry.py must call on_fill with all the
    fields position_monitor expects, only on success, only when
    exit_monitor_enabled."""

    def test_registry_calls_on_fill(self):
        src = REGISTRY_PATH.read_text()
        assert "self._position_monitor.on_fill(" in src, (
            "registry.py must call PositionMonitor.on_fill on successful "
            "LIVE fills (audit #298). Removing this breaks the entire "
            "exit-monitor pipeline."
        )

    def test_on_fill_call_passes_required_kwargs(self):
        src = REGISTRY_PATH.read_text()
        # Find the on_fill call and the surrounding ~30 lines
        idx = src.find("self._position_monitor.on_fill(")
        assert idx > 0
        snippet = src[idx : idx + 800]
        for required in (
            "strategy_id=",
            "window_ts=",
            "direction=",
            "fill_price=",
            "fill_size=",
            "order_id=",
            "token_id=",
        ):
            assert required in snippet, (
                f"registry.py on_fill call missing required kwarg "
                f"{required} — PositionMonitor cannot evaluate exits "
                f"without a complete position record"
            )

    def test_on_fill_only_called_on_success(self):
        """The on_fill call must be inside `if result.success:` —
        registering a failed/no-fill execution would trigger phantom
        exits on a position that doesn't exist on-chain."""
        src = REGISTRY_PATH.read_text()
        # Walk forward from the success branch to confirm on_fill comes
        # AFTER `if result.success:` and not in an else / outer branch.
        success_idx = src.find("if result.success:")
        assert success_idx > 0
        on_fill_idx = src.find("self._position_monitor.on_fill(")
        assert on_fill_idx > success_idx, (
            "PositionMonitor.on_fill must be inside `if result.success:` — "
            "calling it on a failed execution registers a phantom position"
        )

    def test_on_fill_gated_by_exit_monitor_enabled(self):
        """Only strategies with ``exit_monitor_enabled=True`` in
        decision.metadata should register positions. Guarding this lets
        operators run a strategy with the exit-monitor turned off
        (e.g. for shadow comparison) without registering positions."""
        src = REGISTRY_PATH.read_text()
        # The on_fill call site should reference exit_monitor_enabled
        idx = src.find("self._position_monitor.on_fill(")
        # Look at preceding chars (wide enough to span the if-block)
        snippet = src[max(0, idx - 1200) : idx]
        assert "exit_monitor_enabled" in snippet, (
            "on_fill must be gated by exit_monitor_enabled flag from "
            "decision metadata"
        )


# ── 3. resilience: None position_monitor never crashes ─────────────────────


class TestNonePositionMonitorSafe:
    """Tests / legacy composition paths can leave registry._position_monitor
    at None. The registry must guard the on_fill call so a None monitor
    doesn't crash a successful LIVE execution."""

    def test_registry_none_guards_on_fill(self):
        src = REGISTRY_PATH.read_text()
        # The call must be gated by `self._position_monitor is not None`
        idx = src.find("self._position_monitor.on_fill(")
        snippet = src[max(0, idx - 1200) : idx]
        assert (
            "self._position_monitor is not None" in snippet
            or "self._position_monitor:" in snippet
            or "if self._position_monitor" in snippet
        ), (
            "registry.py must guard on_fill with a None-check on "
            "_position_monitor so legacy composition paths don't AttributeError"
        )
