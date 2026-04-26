"""Regression tests for StrategyRegistry paper_mode initialization (audit #318).

Audit #318: ``StrategyRegistry.__init__`` defaults ``_paper_mode = True``
("safe"). The flag is only flipped via ``set_paper_mode()`` from the
mode-switch handler in ``runtime.py``. On a clean LIVE startup, no
mode-switch fires (engine boots straight into LIVE with no prior PAPER
state to switch from), so the registry stays paper forever and
``execute_uc`` never runs — ``not self._paper_mode`` gate at line 627
of ``registry.py`` blocks all executions.

Symptom: TRADE decisions logged, ``registry.executed`` never logs,
fills never happen, dedup ``window_states`` never written. The engine
SILENTLY refuses to trade despite settings.paper_mode=False.

Fix (composition.py): immediately after ``StrategyRegistry(...)`` +
``load_all()``, call ``set_paper_mode(settings.paper_mode)`` so the
registry's view of mode matches the runtime's.

These tests pin two things:
  1. The registry exposes a ``set_paper_mode`` method that mutates state
  2. The ``composition.py`` startup path actually CALLS that method,
     and does so with ``settings.paper_mode`` (not a hard-coded value).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


COMPOSITION_PATH = (
    Path(__file__).parent.parent / "infrastructure" / "composition.py"
)
REGISTRY_PATH = Path(__file__).parent.parent / "strategies" / "registry.py"


def _read(p: Path) -> str:
    return p.read_text()


# ── 1. registry method contract ────────────────────────────────────────────


class TestRegistrySetPaperModeContract:
    """The registry must expose set_paper_mode and have it actually mutate."""

    def test_set_paper_mode_method_exists(self):
        from strategies.registry import StrategyRegistry

        assert hasattr(StrategyRegistry, "set_paper_mode")
        assert callable(getattr(StrategyRegistry, "set_paper_mode"))

    def test_set_paper_mode_flips_internal_flag(self, tmp_path: Path):
        from strategies.registry import StrategyRegistry

        # Build a minimal registry without invoking load_all() — we are
        # testing the flag-mutation contract, not the YAML pipeline.
        reg = StrategyRegistry(
            config_dir=str(tmp_path),
            data_surface=None,
        )
        # Default is True ("safe") — verify pre-condition.
        assert reg._paper_mode is True
        reg.set_paper_mode(False)
        assert reg._paper_mode is False
        reg.set_paper_mode(True)
        assert reg._paper_mode is True

    def test_set_paper_mode_idempotent(self, tmp_path: Path):
        """Calling with the same value twice is a no-op — no exception."""
        from strategies.registry import StrategyRegistry

        reg = StrategyRegistry(
            config_dir=str(tmp_path),
            data_surface=None,
        )
        reg.set_paper_mode(False)
        reg.set_paper_mode(False)
        assert reg._paper_mode is False


# ── 2. composition.py wires the call ───────────────────────────────────────


class TestCompositionCallsSetPaperMode:
    """If composition.py forgets to wire set_paper_mode(settings.paper_mode),
    audit #318 reproduces and v9_lgb_only / v10_lgb_only stop firing fills.

    These tests are static-source checks — they read composition.py and
    verify the wiring. The runtime can't be spun up here (dozens of
    ports), but the source-text invariant is enough to catch regressions.
    """

    def test_composition_calls_set_paper_mode(self):
        src = _read(COMPOSITION_PATH)
        # The exact line we depend on. If a refactor renames the registry
        # field this test should fail — that's fine, the regression check
        # forces a conscious update.
        assert (
            "self._strategy_registry.set_paper_mode(settings.paper_mode)" in src
        ), (
            "composition.py must call StrategyRegistry.set_paper_mode("
            "settings.paper_mode) immediately after construction "
            "(audit #318). Without it the registry's _paper_mode stays "
            "True forever and LIVE strategies never execute."
        )

    def test_set_paper_mode_call_after_load_all(self):
        """Order matters: load_all() must run FIRST so configs exist when
        the paper_mode flag is set. set_paper_mode itself doesn't depend
        on configs but pairing them keeps the wiring clean."""
        src = _read(COMPOSITION_PATH)
        load_idx = src.find("self._strategy_registry.load_all()")
        set_idx = src.find(
            "self._strategy_registry.set_paper_mode(settings.paper_mode)"
        )
        assert load_idx > 0, "load_all() must be present"
        assert set_idx > 0, "set_paper_mode wiring must be present"
        assert set_idx > load_idx, (
            "set_paper_mode must follow load_all() so all loaded configs "
            "are visible when the mode is set"
        )

    def test_set_paper_mode_uses_settings_not_hardcoded(self):
        """No accidental hard-coded ``set_paper_mode(False)`` or
        ``set_paper_mode(True)`` slipping into composition. The argument
        must trace back to ``settings.paper_mode``."""
        src = _read(COMPOSITION_PATH)
        # Allow either explicit or attribute access via settings — but
        # NEVER a literal True/False.
        bad_patterns = [
            r"_strategy_registry\.set_paper_mode\(\s*True\s*\)",
            r"_strategy_registry\.set_paper_mode\(\s*False\s*\)",
        ]
        for pat in bad_patterns:
            assert not re.search(pat, src), (
                f"composition.py contains a hard-coded set_paper_mode call "
                f"matching {pat!r} — this defeats audit #318. The argument "
                f"must come from settings.paper_mode."
            )


# ── 3. the gate that actually uses the flag ────────────────────────────────


class TestPaperModeGate:
    """Defence in depth: the execute gate in registry.py must check the flag."""

    def test_registry_gate_checks_paper_mode(self):
        src = _read(REGISTRY_PATH)
        # The gate condition lives at the top of the LIVE-execute branch.
        assert "not self._paper_mode" in src, (
            "registry.py must gate execute_uc on `not self._paper_mode` — "
            "removing this check causes paper-mode bots to fire real orders"
        )

    def test_default_is_paper_mode_true(self):
        """The constructor default MUST be True ("safe by default"). If
        someone flips this to False, a forgotten test fixture or a
        misconfigured env could fire live orders. Audit #318's fix is to
        eagerly call set_paper_mode at startup, NOT to weaken the safe
        default."""
        src = _read(REGISTRY_PATH)
        assert "self._paper_mode = True" in src, (
            "registry.py default must remain `_paper_mode = True` — fix "
            "audit #318 by wiring set_paper_mode at startup, not by "
            "flipping the default"
        )
