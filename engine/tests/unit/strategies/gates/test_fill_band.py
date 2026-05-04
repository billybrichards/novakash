"""Unit tests for FillBandGate.

Mirrors v9_ensemble.py steps 13–14 (fill_band + per-direction floors).
Strict gate-list strategies were missing this guard before the
2026-05-04 ~10:55 UTC regime flip incident.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from strategies.gates.fill_band import FillBandGate


@dataclass
class _Surface:
    """Minimal stand-in for FullDataSurface — only fields the gate reads."""
    clob_up_ask: Optional[float] = None
    clob_down_ask: Optional[float] = None
    poly_max_entry_price: Optional[float] = None


# ─── construction guards ─────────────────────────────────────────────────


def test_rejects_invalid_direction():
    with pytest.raises(ValueError, match="direction"):
        FillBandGate(direction="BOTH")


def test_rejects_inverted_band():
    with pytest.raises(ValueError, match="min_fill_price"):
        FillBandGate(direction="UP", min_fill_price=0.85, max_fill_price=0.20)


def test_rejects_band_outside_unit_interval():
    with pytest.raises(ValueError):
        FillBandGate(direction="UP", min_fill_price=-0.1, max_fill_price=0.5)
    with pytest.raises(ValueError):
        FillBandGate(direction="UP", min_fill_price=0.1, max_fill_price=1.5)


def test_default_floors_match_v9_ensemble_legacy_knobs():
    """Defaults: UP min=0.20, DOWN min=0.15, max=0.82 — matches the
    legacy v9_ensemble gate_params at the time this gate was extracted."""
    g_up = FillBandGate(direction="UP")
    g_dn = FillBandGate(direction="DOWN")
    assert g_up._min == 0.20
    assert g_dn._min == 0.15
    assert g_up._max == 0.82
    assert g_dn._max == 0.82


# ─── happy path ──────────────────────────────────────────────────────────


def test_pass_when_up_ask_in_band():
    g = FillBandGate(direction="UP", min_fill_price=0.20, max_fill_price=0.82)
    surf = _Surface(clob_up_ask=0.45)
    r = g.evaluate(surf)
    assert r.passed is True
    assert r.data["fill_price"] == 0.45
    assert r.data["source"] == "clob"


def test_pass_when_down_ask_in_band():
    g = FillBandGate(direction="DOWN", min_fill_price=0.15, max_fill_price=0.82)
    surf = _Surface(clob_down_ask=0.55)
    r = g.evaluate(surf)
    assert r.passed is True


def test_pass_at_floor_inclusive():
    g = FillBandGate(direction="UP", min_fill_price=0.20, max_fill_price=0.82)
    surf = _Surface(clob_up_ask=0.20)
    r = g.evaluate(surf)
    assert r.passed is True


def test_pass_at_ceiling_inclusive():
    g = FillBandGate(direction="UP", min_fill_price=0.20, max_fill_price=0.82)
    surf = _Surface(clob_up_ask=0.82)
    r = g.evaluate(surf)
    assert r.passed is True


# ─── failure paths ───────────────────────────────────────────────────────


def test_fail_when_up_ask_below_floor():
    g = FillBandGate(direction="UP", min_fill_price=0.20, max_fill_price=0.82)
    surf = _Surface(clob_up_ask=0.18)
    r = g.evaluate(surf)
    assert r.passed is False
    assert "< floor" in r.reason


def test_fail_when_down_ask_below_floor():
    g = FillBandGate(direction="DOWN", min_fill_price=0.15, max_fill_price=0.82)
    surf = _Surface(clob_down_ask=0.10)
    r = g.evaluate(surf)
    assert r.passed is False
    assert "< floor" in r.reason


def test_fail_when_up_ask_above_ceiling():
    g = FillBandGate(direction="UP", min_fill_price=0.20, max_fill_price=0.82)
    surf = _Surface(clob_up_ask=0.95)
    r = g.evaluate(surf)
    assert r.passed is False
    assert "> ceiling" in r.reason


def test_fail_when_clob_ask_missing_and_require_clob_true():
    g = FillBandGate(direction="UP", require_clob=True)
    surf = _Surface(clob_up_ask=None, poly_max_entry_price=0.50)
    r = g.evaluate(surf)
    assert r.passed is False
    assert r.data["source"] == "none"


def test_fallback_to_poly_max_entry_when_require_clob_false():
    """v9_ensemble parity: when CLOB ask is missing and require_clob is
    False, use poly_max_entry_price as the comparison."""
    g = FillBandGate(direction="UP", require_clob=False)
    surf = _Surface(clob_up_ask=None, poly_max_entry_price=0.50)
    r = g.evaluate(surf)
    assert r.passed is True
    assert r.data["source"] == "poly_max_entry"


# ─── direction routing ──────────────────────────────────────────────────


def test_up_gate_ignores_down_ask():
    """An UP-gate must read clob_up_ask, not clob_down_ask. Regression
    guard: a typo wiring the wrong attr would silently let trades through
    against an out-of-band ask in the opposite leg."""
    g = FillBandGate(direction="UP", min_fill_price=0.20, max_fill_price=0.82)
    surf = _Surface(clob_up_ask=None, clob_down_ask=0.50)
    r = g.evaluate(surf)
    assert r.passed is False  # up_ask is None → fail (require_clob default)
    assert r.data["source"] == "none"


def test_down_gate_ignores_up_ask():
    g = FillBandGate(direction="DOWN", min_fill_price=0.15, max_fill_price=0.82)
    surf = _Surface(clob_up_ask=0.50, clob_down_ask=None)
    r = g.evaluate(surf)
    assert r.passed is False
    assert r.data["source"] == "none"


# ─── registry wiring smoke test ──────────────────────────────────────────


def test_registry_resolves_fill_band_gate_type():
    """Confirms the gate is wired into the registry and constructible
    from a plain YAML param dict."""
    from strategies.registry import _GATE_REGISTRY, _register_gates

    _register_gates()
    cls = _GATE_REGISTRY.get("fill_band")
    assert cls is FillBandGate
    g = cls(**{"direction": "UP", "min_fill_price": 0.20, "max_fill_price": 0.82})
    assert g.name == "fill_band"
