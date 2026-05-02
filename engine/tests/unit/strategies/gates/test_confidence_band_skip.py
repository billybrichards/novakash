"""Unit tests for ConfidenceBandSkipGate.

Data-grounding: each ``skip_band`` listed below maps to a row range in
docs/data-backed-strategy-plan-2026-05-02/sweeps/04_conviction_*_btc_5m.tsv —
the per-model "noise zone" where WR-if-bet sits at or below coinflip.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from strategies.gates.confidence_band_skip import ConfidenceBandSkipGate


@dataclass
class _Surface:
    probability_lgb: Optional[float] = None        # v9
    probability_lgb_v9_1: Optional[float] = None
    probability_lgb_v10: Optional[float] = None
    probability_lgb_v12: Optional[float] = None
    probability_classifier: Optional[float] = None


# ─── construction ────────────────────────────────────────────────────────


def test_rejects_unknown_model():
    with pytest.raises(ValueError, match="unknown model"):
        ConfidenceBandSkipGate(model="bogus", skip_band=[0.4, 0.6])


def test_rejects_malformed_band():
    with pytest.raises(ValueError, match="must be"):
        ConfidenceBandSkipGate(model="v9", skip_band=[0.5])
    with pytest.raises(ValueError, match="must be"):
        ConfidenceBandSkipGate(model="v9", skip_band=[0.5, 0.4, 0.3])


def test_rejects_inverted_band():
    with pytest.raises(ValueError):
        ConfidenceBandSkipGate(model="v9", skip_band=[0.7, 0.3])  # lo > hi


def test_rejects_out_of_range_band():
    with pytest.raises(ValueError):
        ConfidenceBandSkipGate(model="v9", skip_band=[-0.1, 0.5])
    with pytest.raises(ValueError):
        ConfidenceBandSkipGate(model="v9", skip_band=[0.5, 1.5])


# ─── v12 [0.30, 0.70] band — the documented data-backed case ────────────


def test_v12_extreme_band_passes_below_30():
    """Sweep 4-v12: p<0.30 → 80.8% DOWN WR (n=52). Tradeable."""
    g = ConfidenceBandSkipGate(model="v12", skip_band=[0.30, 0.70])
    r = g.evaluate(_Surface(probability_lgb_v12=0.25))
    assert r.passed is True


def test_v12_extreme_band_passes_above_70():
    """Sweep 4-v12: p>=0.70 → 83% UP WR (n=59). Tradeable."""
    g = ConfidenceBandSkipGate(model="v12", skip_band=[0.30, 0.70])
    r = g.evaluate(_Surface(probability_lgb_v12=0.75))
    assert r.passed is True


def test_v12_extreme_band_skips_inside():
    """Sweep 4-v12: 0.30-0.70 mostly noise zone (33-58% directional WR)."""
    g = ConfidenceBandSkipGate(model="v12", skip_band=[0.30, 0.70])
    for p in (0.30, 0.45, 0.50, 0.55, 0.65, 0.70):
        r = g.evaluate(_Surface(probability_lgb_v12=p))
        assert r.passed is False, f"p={p} should be in skip band"
        assert "skip_band" in r.reason


def test_inclusive_at_lower_bound():
    """exactly at lower bound is INSIDE the band → skip"""
    g = ConfidenceBandSkipGate(model="v9", skip_band=[0.40, 0.65])
    r = g.evaluate(_Surface(probability_lgb=0.40))
    assert r.passed is False


def test_inclusive_at_upper_bound():
    g = ConfidenceBandSkipGate(model="v9", skip_band=[0.40, 0.65])
    r = g.evaluate(_Surface(probability_lgb=0.65))
    assert r.passed is False


def test_just_outside_passes():
    g = ConfidenceBandSkipGate(model="v9", skip_band=[0.40, 0.65])
    r = g.evaluate(_Surface(probability_lgb=0.39))
    assert r.passed is True
    r = g.evaluate(_Surface(probability_lgb=0.66))
    assert r.passed is True


# ─── data unavailability ─────────────────────────────────────────────────


def test_fails_when_model_unavailable():
    """Strategy USING this gate explicitly requires the model. Different
    semantic from disagreement_veto's "pass on unavailability"."""
    g = ConfidenceBandSkipGate(model="v9", skip_band=[0.40, 0.65])
    r = g.evaluate(_Surface())
    assert r.passed is False
    assert "not populated" in r.reason


# ─── classifier specific (post-iso narrower noise zone) ──────────────────


def test_classifier_narrow_band():
    """Sweep 4-classifier: tradable WR starts at 0.60 (UP) and 0.40 (DOWN)."""
    g = ConfidenceBandSkipGate(model="classifier", skip_band=[0.45, 0.55])
    # Just outside the narrow band — should pass
    assert g.evaluate(_Surface(probability_classifier=0.44)).passed is True
    assert g.evaluate(_Surface(probability_classifier=0.56)).passed is True
    # Inside the narrow band — should skip
    assert g.evaluate(_Surface(probability_classifier=0.50)).passed is False
