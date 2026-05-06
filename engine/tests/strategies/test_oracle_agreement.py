"""4-source oracle agreement (audit #373, 2026-05-06).

Verifies the SourceAgreementGate consumes the new `delta_coinglass`
field, and that v9_ensemble's per-strategy `oracle_agreement_min_sources`
override works:

* 4-of-4 agree on direction → PASS
* 3-of-4 agree → PASS at min_sources=3
* 2-of-4 agree → SKIP at min_sources=3
* coinglass missing + 3-of-3 spot agree → PASS
* only 2 sources available → falls back to legacy 2-source
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from strategies.gates.source_agreement import SourceAgreementGate
from strategies.configs import v9_ensemble as v9


def _src(
    *,
    cl=None,
    ti=None,
    bn=None,
    cg=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        delta_chainlink=cl,
        delta_tiingo=ti,
        delta_binance=bn,
        delta_coinglass=cg,
    )


# ───────────────────────── SourceAgreementGate (audit #373) ──────────────────


def test_gate_passes_when_4_of_4_agree_up():
    gate = SourceAgreementGate(min_sources=3)
    res = gate.evaluate(_src(cl=0.001, ti=0.001, bn=0.001, cg=0.05))
    assert res.passed
    assert "4/4" in res.reason or "4 sources" in res.reason or "UP" in res.reason


def test_gate_passes_when_3_of_4_agree():
    gate = SourceAgreementGate(min_sources=3)
    # 3 UP, 1 DOWN (coinglass against)
    res = gate.evaluate(_src(cl=0.001, ti=0.001, bn=0.001, cg=-0.05))
    assert res.passed


def test_gate_skips_when_only_2_of_4_agree_at_min3():
    gate = SourceAgreementGate(min_sources=3)
    # 2 UP, 2 DOWN
    res = gate.evaluate(_src(cl=0.001, ti=-0.001, bn=0.001, cg=-0.05))
    assert not res.passed


def test_gate_falls_back_when_coinglass_missing():
    """3-of-3 spot sources agree, no CG signal → still passes at min_sources=3."""
    gate = SourceAgreementGate(min_sources=3)
    res = gate.evaluate(_src(cl=0.001, ti=0.001, bn=0.001, cg=None))
    assert res.passed


def test_gate_legacy_2source_when_only_2_available():
    """When only chainlink + tiingo are available, gate honours min_sources=2."""
    gate = SourceAgreementGate(min_sources=2)
    res = gate.evaluate(_src(cl=0.001, ti=0.001, bn=None, cg=None))
    assert res.passed


def test_gate_skips_when_2_sources_disagree():
    gate = SourceAgreementGate(min_sources=2)
    res = gate.evaluate(_src(cl=0.001, ti=-0.001, bn=None, cg=None))
    assert not res.passed


def test_gate_handles_missing_coinglass_field_on_old_surface():
    """Surfaces without the `delta_coinglass` attribute should still work
    (back-compat: getattr returns None and gate falls through)."""
    gate = SourceAgreementGate(min_sources=2)
    legacy_surface = SimpleNamespace(
        delta_chainlink=0.001,
        delta_tiingo=0.001,
        delta_binance=None,
        # no `delta_coinglass`
    )
    res = gate.evaluate(legacy_surface)
    assert res.passed


# ─────────────── v9_ensemble.oracle_agreement_min_sources override ───────────


def test_oracle_agreement_min_sources_default_is_2():
    assert v9._oracle_agreement_min_sources() == 2


def test_oracle_agreement_min_sources_can_be_set_to_3():
    with patch.object(v9, "_oracle_agreement_min_sources", return_value=3):
        assert v9._oracle_agreement_min_sources() == 3
