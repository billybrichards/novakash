"""VPINGate tests — constructor behaviour + runtime override (gate_params).

Covers:
* Constructor-only behaviour (preserved, backward compatible)
* Runtime override flips block_cascade from True to False
* Runtime override flips min threshold
* Runtime overrides are isolated per-evaluation (context var resets)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from strategies import gate_params as _gp
from strategies.gates.vpin_gate import VPINGate


# ── helpers ──────────────────────────────────────────────────────────────────

def _surface(vpin=None, regime=None):
    return SimpleNamespace(vpin=vpin, regime=regime)


# ── Constructor-only behaviour (backward compat) ──────────────────────────────

class TestConstructorBehaviour:
    def test_vpin_none_fails(self):
        gate = VPINGate(min=0.40, block_cascade=True)
        res = gate.evaluate(_surface(vpin=None))
        assert not res.passed
        assert "None" in res.reason

    def test_vpin_below_min_fails(self):
        gate = VPINGate(min=0.40, block_cascade=True)
        res = gate.evaluate(_surface(vpin=0.39, regime="NORMAL"))
        assert not res.passed
        assert "0.390" in res.reason

    def test_vpin_at_min_passes(self):
        gate = VPINGate(min=0.40, block_cascade=True)
        res = gate.evaluate(_surface(vpin=0.40, regime="NORMAL"))
        assert res.passed

    def test_cascade_blocked_when_block_cascade_true(self):
        gate = VPINGate(min=0.40, block_cascade=True)
        res = gate.evaluate(_surface(vpin=0.65, regime="CASCADE"))
        assert not res.passed
        assert "CASCADE" in res.reason

    def test_cascade_passes_when_block_cascade_false(self):
        gate = VPINGate(min=0.40, block_cascade=False)
        res = gate.evaluate(_surface(vpin=0.65, regime="CASCADE"))
        assert res.passed

    def test_non_cascade_regime_passes(self):
        gate = VPINGate(min=0.40, block_cascade=True)
        for regime in ("CALM", "NORMAL", "TRANSITION", None):
            res = gate.evaluate(_surface(vpin=0.65, regime=regime))
            assert res.passed, f"Expected pass for regime={regime!r}"

    def test_regime_check_is_case_insensitive(self):
        gate = VPINGate(min=0.40, block_cascade=True)
        res = gate.evaluate(_surface(vpin=0.65, regime="cascade"))
        assert not res.passed


# ── Runtime override: vpin_gate_block_cascade ─────────────────────────────────

class TestRuntimeOverrideBlockCascade:
    def test_override_false_lifts_cascade_block(self):
        """gate_params override block_cascade=False allows CASCADE through."""
        gate = VPINGate(min=0.40, block_cascade=True)  # constructor: block
        token = _gp.set_active({"vpin_gate_block_cascade": False})
        try:
            res = gate.evaluate(_surface(vpin=0.65, regime="CASCADE"))
        finally:
            _gp.reset_active(token)
        assert res.passed, "Runtime override should lift CASCADE block"

    def test_override_true_enforces_cascade_block(self):
        """gate_params override block_cascade=True blocks CASCADE even when ctor=False."""
        gate = VPINGate(min=0.40, block_cascade=False)  # constructor: allow
        token = _gp.set_active({"vpin_gate_block_cascade": True})
        try:
            res = gate.evaluate(_surface(vpin=0.65, regime="CASCADE"))
        finally:
            _gp.reset_active(token)
        assert not res.passed, "Runtime override should enforce CASCADE block"

    def test_no_override_uses_constructor_value(self):
        """Without any gate_params, constructor value is used."""
        gate = VPINGate(min=0.40, block_cascade=True)
        # Ensure clean context (no active params)
        token = _gp.set_active({})
        try:
            res = gate.evaluate(_surface(vpin=0.65, regime="CASCADE"))
        finally:
            _gp.reset_active(token)
        assert not res.passed, "No override → constructor block_cascade=True should block"

    def test_override_is_isolated_after_reset(self):
        """Context var resets properly — override does not leak between calls."""
        gate = VPINGate(min=0.40, block_cascade=True)

        # First call: override lifts the block
        token = _gp.set_active({"vpin_gate_block_cascade": False})
        try:
            res1 = gate.evaluate(_surface(vpin=0.65, regime="CASCADE"))
        finally:
            _gp.reset_active(token)
        assert res1.passed

        # Second call: no override → constructor value takes over again
        res2 = gate.evaluate(_surface(vpin=0.65, regime="CASCADE"))
        assert not res2.passed, "Override must not leak after context reset"


# ── Runtime override: vpin_gate_min ──────────────────────────────────────────

class TestRuntimeOverrideMin:
    def test_override_raises_min_threshold(self):
        """gate_params override vpin_gate_min=0.60 blocks vpin=0.50."""
        gate = VPINGate(min=0.40, block_cascade=False)  # ctor: 0.40 passes
        token = _gp.set_active({"vpin_gate_min": 0.60})
        try:
            res = gate.evaluate(_surface(vpin=0.50, regime="NORMAL"))
        finally:
            _gp.reset_active(token)
        assert not res.passed, "Override min=0.60 should block vpin=0.50"

    def test_override_lowers_min_threshold(self):
        """gate_params override vpin_gate_min=0.20 allows vpin=0.30 through."""
        gate = VPINGate(min=0.40, block_cascade=False)  # ctor: 0.40 blocks 0.30
        token = _gp.set_active({"vpin_gate_min": 0.20})
        try:
            res = gate.evaluate(_surface(vpin=0.30, regime="NORMAL"))
        finally:
            _gp.reset_active(token)
        assert res.passed, "Override min=0.20 should allow vpin=0.30"

    def test_override_min_and_block_cascade_together(self):
        """Both overrides active simultaneously work correctly."""
        gate = VPINGate(min=0.40, block_cascade=True)
        token = _gp.set_active({"vpin_gate_min": 0.20, "vpin_gate_block_cascade": False})
        try:
            res = gate.evaluate(_surface(vpin=0.30, regime="CASCADE"))
        finally:
            _gp.reset_active(token)
        assert res.passed, "Both overrides should allow vpin=0.30 in CASCADE"
