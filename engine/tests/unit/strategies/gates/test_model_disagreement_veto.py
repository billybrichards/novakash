"""Unit tests for ModelDisagreementVetoGate.

Data-grounding: the default ``max_abs_diff=0.20`` is the cutoff at which
sweep 6 (`docs/.../sweeps/06_disagree_v12_v9_btc_5m.tsv`) shows
v12 starts winning over v9. Below that, the strategy's anchor model
should still be trusted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from strategies.gates.model_disagreement_veto import ModelDisagreementVetoGate


@dataclass
class _Surface:
    probability_lgb: Optional[float] = None        # v9
    probability_lgb_v9_1: Optional[float] = None
    probability_lgb_v10: Optional[float] = None
    probability_lgb_v12: Optional[float] = None
    probability_classifier: Optional[float] = None


# ─── construction ────────────────────────────────────────────────────────


def test_rejects_unknown_primary():
    with pytest.raises(ValueError, match="primary"):
        ModelDisagreementVetoGate(primary="bogus", secondary="v12")


def test_rejects_unknown_secondary():
    with pytest.raises(ValueError, match="secondary"):
        ModelDisagreementVetoGate(primary="v9", secondary="bogus")


def test_rejects_same_primary_and_secondary():
    with pytest.raises(ValueError, match="must differ"):
        ModelDisagreementVetoGate(primary="v9", secondary="v9")


def test_rejects_invalid_max_abs_diff():
    with pytest.raises(ValueError):
        ModelDisagreementVetoGate(primary="v9", secondary="v12", max_abs_diff=0.0)
    with pytest.raises(ValueError):
        ModelDisagreementVetoGate(primary="v9", secondary="v12", max_abs_diff=1.5)


# ─── pass / fail behaviour ───────────────────────────────────────────────


def test_passes_when_models_agree_within_threshold():
    """sweep 6 row 0.05-0.10: WR follow-v9=78.3% — keep firing the v9 anchor."""
    g = ModelDisagreementVetoGate(primary="v9", secondary="v12", max_abs_diff=0.20)
    surf = _Surface(probability_lgb=0.65, probability_lgb_v12=0.55)  # diff 0.10
    r = g.evaluate(surf)
    assert r.passed is True
    assert r.data["abs_diff"] == pytest.approx(0.10)


def test_blocks_when_disagreement_at_threshold():
    """At exactly max_abs_diff the gate blocks — sweep 6 ≥0.20 is where
    v12 starts winning over v9."""
    g = ModelDisagreementVetoGate(primary="v9", secondary="v12", max_abs_diff=0.20)
    surf = _Surface(probability_lgb=0.70, probability_lgb_v12=0.50)  # diff 0.20
    r = g.evaluate(surf)
    assert r.passed is False
    assert "0.200" in r.reason
    assert ">=" in r.reason


def test_blocks_when_disagreement_exceeds_threshold():
    g = ModelDisagreementVetoGate(primary="v9", secondary="v12", max_abs_diff=0.20)
    surf = _Surface(probability_lgb=0.80, probability_lgb_v12=0.50)  # diff 0.30
    r = g.evaluate(surf)
    assert r.passed is False
    assert r.data["abs_diff"] == pytest.approx(0.30)


def test_passes_when_either_model_unavailable():
    """During cold start a missing probability shouldn't block firing —
    the strategy's other gates ensure the primary is actually populated."""
    g = ModelDisagreementVetoGate(primary="v9", secondary="v12", max_abs_diff=0.20)
    r = g.evaluate(_Surface(probability_lgb=0.65))           # v12 missing
    assert r.passed is True
    r = g.evaluate(_Surface(probability_lgb_v12=0.50))       # v9 missing
    assert r.passed is True
    r = g.evaluate(_Surface())                                # both missing
    assert r.passed is True


def test_works_with_classifier_as_secondary():
    """Pattern: classifier as the regime-drift detector vetoing a v9.1 anchor."""
    g = ModelDisagreementVetoGate(
        primary="v9_1", secondary="classifier", max_abs_diff=0.20,
    )
    surf = _Surface(probability_lgb_v9_1=0.65, probability_classifier=0.30)
    r = g.evaluate(surf)
    assert r.passed is False
    assert r.data["abs_diff"] == pytest.approx(0.35)


def test_symmetric_around_zero_difference():
    """Direction of the diff doesn't matter — the gate uses abs()."""
    g = ModelDisagreementVetoGate(primary="v9", secondary="v12", max_abs_diff=0.20)
    surf_a = _Surface(probability_lgb=0.70, probability_lgb_v12=0.40)  # primary higher
    surf_b = _Surface(probability_lgb=0.40, probability_lgb_v12=0.70)  # secondary higher
    assert g.evaluate(surf_a).passed is False
    assert g.evaluate(surf_b).passed is False
