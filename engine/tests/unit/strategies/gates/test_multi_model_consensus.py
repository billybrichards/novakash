"""Unit tests for MultiModelConsensusGate.

Data-grounding: every pass/fail expectation maps to a row in
docs/data-backed-strategy-plan-2026-05-02/sweeps/11_consensus_simulation_5m.tsv.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from strategies.gates.multi_model_consensus import (
    MultiModelConsensusGate,
    _MODEL_FIELD_MAP,
)


@dataclass
class _Surface:
    """Minimal stand-in for FullDataSurface — only fields the gate reads."""
    probability_lgb: Optional[float] = None        # v9
    probability_lgb_v9_1: Optional[float] = None
    probability_lgb_v10: Optional[float] = None
    probability_lgb_v12: Optional[float] = None
    probability_classifier: Optional[float] = None


# ─── construction ────────────────────────────────────────────────────────


def test_rejects_unknown_model():
    with pytest.raises(ValueError, match="unknown model"):
        MultiModelConsensusGate(models=["v9", "bogus"], min_votes=2)


def test_rejects_min_votes_out_of_range():
    with pytest.raises(ValueError, match="min_votes"):
        MultiModelConsensusGate(models=["v9", "v10"], min_votes=3)
    with pytest.raises(ValueError, match="min_votes"):
        MultiModelConsensusGate(models=["v9", "v10"], min_votes=0)


def test_rejects_invalid_direction():
    with pytest.raises(ValueError, match="direction"):
        MultiModelConsensusGate(
            models=["v9", "v10"], min_votes=1, direction="SIDEWAYS"
        )


def test_rejects_threshold_out_of_range():
    with pytest.raises(ValueError, match="vote_threshold_up"):
        MultiModelConsensusGate(
            models=["v9", "v10"], min_votes=1, vote_threshold_up=0.4
        )


def test_model_field_map_covers_all_lgbs_plus_classifier():
    assert set(_MODEL_FIELD_MAP) == {"v9", "v9_1", "v10", "v12", "classifier"}


# ─── 5-of-5 / 4-of-5 / 3-of-5 UP votes (sweep 11 row interpretation) ────


def test_3of5_up_passes_at_threshold():
    """Sweep 11: 3-of-5 UP-vote → 80.7% WR (n=135). The data-backed pass."""
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10", "v12", "classifier"],
        min_votes=3, direction="UP", vote_threshold=0.55,
    )
    # exactly 3 of 5 above 0.55, others below:
    surf = _Surface(
        probability_lgb=0.62,         # UP
        probability_lgb_v9_1=0.60,    # UP
        probability_lgb_v10=0.58,     # UP
        probability_lgb_v12=0.50,     # neutral
        probability_classifier=0.50,  # neutral
    )
    r = g.evaluate(surf)
    assert r.passed is True
    assert r.data["votes_up"] == 3
    assert r.data["votes_dn"] == 0
    assert r.data["available"] == 5


def test_2of5_up_fails_below_threshold():
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10", "v12", "classifier"],
        min_votes=3, direction="UP",
    )
    surf = _Surface(
        probability_lgb=0.62, probability_lgb_v9_1=0.62,
        probability_lgb_v10=0.50, probability_lgb_v12=0.50,
        probability_classifier=0.50,
    )
    r = g.evaluate(surf)
    assert r.passed is False
    assert r.data["votes_up"] == 2


def test_4of5_dn_passes_strong_consensus():
    """Sweep 11: 4-of-5 DOWN → 91.7% WR (n=24). Highest-conviction signal."""
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10", "v12", "classifier"],
        min_votes=4, direction="DOWN", vote_threshold=0.55,
    )
    surf = _Surface(
        probability_lgb=0.30, probability_lgb_v9_1=0.32,
        probability_lgb_v10=0.28, probability_lgb_v12=0.40,
        probability_classifier=0.50,  # only 4 of 5 vote DOWN
    )
    r = g.evaluate(surf)
    assert r.passed is True
    assert r.data["votes_dn"] == 4


# ─── either-side mode (no direction set) ────────────────────────────────


def test_either_side_passes_with_up_majority_and_attaches_direction():
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10", "v12", "classifier"],
        min_votes=3,
    )
    surf = _Surface(
        probability_lgb=0.65, probability_lgb_v9_1=0.70,
        probability_lgb_v10=0.60, probability_lgb_v12=0.50,
        probability_classifier=0.50,
    )
    r = g.evaluate(surf)
    assert r.passed is True
    assert r.data.get("direction") == "UP"
    assert r.data["votes_up"] == 3


def test_either_side_passes_with_down_majority():
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10", "v12", "classifier"],
        min_votes=3,
    )
    surf = _Surface(
        probability_lgb=0.30, probability_lgb_v9_1=0.30,
        probability_lgb_v10=0.40, probability_lgb_v12=0.50,
        probability_classifier=0.50,
    )
    r = g.evaluate(surf)
    assert r.passed is True
    assert r.data.get("direction") == "DOWN"


def test_either_side_no_majority_fails():
    """Sweep 11: no_majority → 45.9% UP outcome (~coinflip). Data says skip."""
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10", "v12", "classifier"],
        min_votes=3,
    )
    surf = _Surface(
        probability_lgb=0.55, probability_lgb_v9_1=0.50,
        probability_lgb_v10=0.50, probability_lgb_v12=0.45,
        probability_classifier=0.50,
    )
    r = g.evaluate(surf)
    assert r.passed is False
    assert "no majority" in r.reason


# ─── available counting (None probabilities skipped) ─────────────────────


def test_none_probabilities_excluded_from_count():
    """A model with None probability should not contribute UP or DOWN.
    Available count tracks how many models actually contributed."""
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10", "v12", "classifier"],
        min_votes=3, direction="UP",
    )
    surf = _Surface(
        probability_lgb=0.65,
        probability_lgb_v9_1=0.65,
        probability_lgb_v10=0.65,
        probability_lgb_v12=None,           # not loaded
        probability_classifier=None,         # not loaded
    )
    r = g.evaluate(surf)
    assert r.passed is True
    assert r.data["votes_up"] == 3
    assert r.data["available"] == 3


def test_eth_xrp_path_with_only_classifier_populated():
    """For ETH/XRP currently only classifier surfaces. With min_votes=1
    and direction=UP a single high-conviction classifier vote should pass."""
    g = MultiModelConsensusGate(
        models=["classifier"], min_votes=1, direction="UP",
        vote_threshold_up=0.62,
    )
    surf = _Surface(probability_classifier=0.65)
    r = g.evaluate(surf)
    assert r.passed is True


def test_all_models_none_fails_gracefully():
    """During cold start every probability may be None. Gate should
    fail (no votes), NOT raise."""
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10", "v12", "classifier"],
        min_votes=3, direction="UP",
    )
    r = g.evaluate(_Surface())
    assert r.passed is False
    assert r.data["available"] == 0


# ─── threshold semantics ─────────────────────────────────────────────────


def test_inclusive_at_up_threshold():
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10"], min_votes=3, direction="UP",
        vote_threshold_up=0.55,
    )
    # exactly at threshold counts as UP vote
    surf = _Surface(
        probability_lgb=0.55, probability_lgb_v9_1=0.55,
        probability_lgb_v10=0.55,
    )
    r = g.evaluate(surf)
    assert r.passed is True


def test_inclusive_at_down_threshold():
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10"], min_votes=3, direction="DOWN",
        vote_threshold=0.55,
    )
    surf = _Surface(
        probability_lgb=0.45, probability_lgb_v9_1=0.45,
        probability_lgb_v10=0.45,
    )
    r = g.evaluate(surf)
    assert r.passed is True


def test_just_inside_no_trade_band_no_vote():
    """0.46 / 0.54 are inside the band, neither UP nor DOWN."""
    g = MultiModelConsensusGate(
        models=["v9", "v9_1", "v10"], min_votes=1,
    )
    surf = _Surface(
        probability_lgb=0.54, probability_lgb_v9_1=0.46,
        probability_lgb_v10=0.50,
    )
    r = g.evaluate(surf)
    assert r.passed is False
    assert r.data["votes_up"] == 0 and r.data["votes_dn"] == 0
    assert r.data["available"] == 3
