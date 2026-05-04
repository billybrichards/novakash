"""Unit tests for OracleDirectionGate.

Closes the safety gap surfaced by the 2026-05-04 ~10:55 UTC regime flip
(see hub notes #330, #331). The dict-style v9_ensemble check that this
gate mirrors is in ``engine/strategies/configs/v9_ensemble.py`` step 12.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from strategies.gates.oracle_direction import OracleDirectionGate


@dataclass
class _Surface:
    """Minimal stand-in for FullDataSurface — only fields the gate reads."""
    delta_chainlink: Optional[float] = None
    delta_tiingo: Optional[float] = None
    delta_binance: Optional[float] = None  # not consumed but kept for parity


# ─── construction guards ─────────────────────────────────────────────────


def test_rejects_invalid_direction():
    with pytest.raises(ValueError, match="direction"):
        OracleDirectionGate(direction="SIDEWAYS")


def test_rejects_lowercase_direction():
    with pytest.raises(ValueError, match="direction"):
        OracleDirectionGate(direction="up")


# ─── strict (require_both=True) — defaults to the strict mode ────────────


def test_pass_when_both_oracles_agree_with_up():
    g = OracleDirectionGate(direction="UP")
    surf = _Surface(delta_chainlink=0.001, delta_tiingo=0.0008)
    r = g.evaluate(surf)
    assert r.passed is True
    assert r.data["chainlink_dir"] == "UP"
    assert r.data["tiingo_dir"] == "UP"


def test_pass_when_both_oracles_agree_with_down():
    g = OracleDirectionGate(direction="DOWN")
    surf = _Surface(delta_chainlink=-0.0012, delta_tiingo=-0.0009)
    r = g.evaluate(surf)
    assert r.passed is True
    assert r.data["chainlink_dir"] == "DOWN"
    assert r.data["tiingo_dir"] == "DOWN"


def test_fail_when_chainlink_disagrees_with_up_strategy():
    """The exact pattern that fired at 2026-05-04 ~10:55 UTC: chainlink
    delta crossed sign before tiingo, strategy was DOWN-aligned but
    chainlink had already flipped UP."""
    g = OracleDirectionGate(direction="DOWN")
    surf = _Surface(delta_chainlink=+0.0005, delta_tiingo=-0.0008)
    r = g.evaluate(surf)
    assert r.passed is False
    assert "chainlink" in r.data["disagreers"]
    assert "tiingo" not in r.data["disagreers"]


def test_fail_when_tiingo_disagrees():
    g = OracleDirectionGate(direction="UP")
    surf = _Surface(delta_chainlink=+0.0010, delta_tiingo=-0.0005)
    r = g.evaluate(surf)
    assert r.passed is False
    assert r.data["disagreers"] == ["tiingo"]


def test_fail_when_both_disagree():
    g = OracleDirectionGate(direction="UP")
    surf = _Surface(delta_chainlink=-0.0010, delta_tiingo=-0.0005)
    r = g.evaluate(surf)
    assert r.passed is False
    assert set(r.data["disagreers"]) == {"chainlink", "tiingo"}


def test_fail_when_chainlink_null_in_strict_mode():
    g = OracleDirectionGate(direction="UP", require_both=True)
    surf = _Surface(delta_chainlink=None, delta_tiingo=+0.0010)
    r = g.evaluate(surf)
    assert r.passed is False
    assert "chainlink" in r.data["missing"]


def test_fail_when_tiingo_null_in_strict_mode():
    g = OracleDirectionGate(direction="DOWN", require_both=True)
    surf = _Surface(delta_chainlink=-0.0010, delta_tiingo=None)
    r = g.evaluate(surf)
    assert r.passed is False
    assert "tiingo" in r.data["missing"]


def test_fail_when_both_null_in_strict_mode():
    g = OracleDirectionGate(direction="UP", require_both=True)
    surf = _Surface()
    r = g.evaluate(surf)
    assert r.passed is False
    assert set(r.data["missing"]) == {"chainlink", "tiingo"}


# ─── lenient (require_both=False) ────────────────────────────────────────


def test_lenient_passes_with_only_one_oracle_when_agreeing():
    g = OracleDirectionGate(direction="UP", require_both=False)
    surf = _Surface(delta_chainlink=+0.0010, delta_tiingo=None)
    r = g.evaluate(surf)
    assert r.passed is True


def test_lenient_still_fails_on_active_disagreement():
    """Even with require_both=False, a populated oracle that disagrees
    with the strategy direction is fatal — we never tolerate active
    disagreement, only missing data."""
    g = OracleDirectionGate(direction="UP", require_both=False)
    surf = _Surface(delta_chainlink=+0.0010, delta_tiingo=-0.0005)
    r = g.evaluate(surf)
    assert r.passed is False
    assert r.data["disagreers"] == ["tiingo"]


def test_lenient_with_no_oracles_passes_vacuously():
    """Cold start: no oracles populated. Lenient mode treats that as
    "no information, no veto" — the timing / consecutive_pass_ticks
    gates handle cold-start blocking separately."""
    g = OracleDirectionGate(direction="UP", require_both=False)
    surf = _Surface()
    r = g.evaluate(surf)
    assert r.passed is True


# ─── zero handling ───────────────────────────────────────────────────────


def test_zero_delta_treated_as_pass_by_default():
    """zero_is_pass=True (default): exactly-0.0 delta matches whichever
    direction was asked for. Avoids spurious skips on quiet ticks."""
    g = OracleDirectionGate(direction="UP")
    surf = _Surface(delta_chainlink=0.0, delta_tiingo=+0.0010)
    r = g.evaluate(surf)
    assert r.passed is True


def test_zero_delta_treated_as_down_when_zero_is_pass_off():
    g = OracleDirectionGate(direction="UP", zero_is_pass=False)
    surf = _Surface(delta_chainlink=0.0, delta_tiingo=+0.0010)
    r = g.evaluate(surf)
    assert r.passed is False  # 0.0 → DOWN, disagrees with UP
    assert "chainlink" in r.data["disagreers"]


# ─── regression: the exact 2026-05-04 incident ────────────────────────────


def test_regression_2026_05_04_regime_flip_blocks_strict_dn():
    """At ~10:55 UTC chainlink had ticked +0.0006 while tiingo still
    showed -0.0009 (regime mid-flip). The strict gate-list DN strategies
    had no oracle check and fired into the disagreement — losses ensued.
    With this gate enabled the trade should skip."""
    g = OracleDirectionGate(direction="DOWN")
    surf = _Surface(delta_chainlink=+0.0006, delta_tiingo=-0.0009)
    r = g.evaluate(surf)
    assert r.passed is False
    assert r.data["chainlink_dir"] == "UP"
    assert r.data["tiingo_dir"] == "DOWN"


def test_regression_2026_05_04_regime_flip_blocks_strict_up_mirror():
    """The mirror-image case: an UP-strategy when oracles split UP/DOWN."""
    g = OracleDirectionGate(direction="UP")
    surf = _Surface(delta_chainlink=-0.0008, delta_tiingo=+0.0006)
    r = g.evaluate(surf)
    assert r.passed is False


# ─── registry wiring smoke test ──────────────────────────────────────────


def test_registry_resolves_oracle_direction_gate_type():
    """Confirms the gate is wired into the registry and constructible
    from a plain YAML param dict."""
    from strategies.registry import _GATE_REGISTRY, _register_gates

    _register_gates()
    cls = _GATE_REGISTRY.get("oracle_direction")
    assert cls is OracleDirectionGate
    # Construct as if from YAML.
    g = cls(**{"direction": "DOWN"})
    assert g.name == "oracle_direction"
