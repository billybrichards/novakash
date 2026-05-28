"""Unit tests for cross-strategy mutex-group resolver (FIX 2)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

os.environ.setdefault(
    "DATABASE_URL", "postgresql://test:test@localhost:5432/test"
)

from domain.value_objects import StrategyDecision  # noqa: E402
from strategies.mutex_resolver import (  # noqa: E402
    resolve_mutex_groups,
)


def _trade(strategy_id: str, score: float, *, direction: str = "UP",
           mutex_group: str = "tickformer") -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence="HIGH",
        confidence_score=score,
        entry_cap=0.93,
        collateral_pct=0.025,
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        entry_reason=f"{strategy_id}_pass",
        skip_reason=None,
        metadata={"mutex_group": mutex_group, "would_trade": True},
        gtc_cap=0.96,
    )


def _skip(strategy_id: str, reason: str = "shadow_only_no_trade",
          mutex_group: str = "tickformer") -> StrategyDecision:
    return StrategyDecision(
        action="SKIP",
        direction=None,
        confidence=None,
        confidence_score=0.0,
        entry_cap=0.0,
        collateral_pct=0.0,
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        entry_reason="",
        skip_reason=reason,
        metadata={"mutex_group": mutex_group},
    )


# ── Up-side: 3 contenders, highest score wins ─────────────────────────


def test_three_up_contenders_highest_conviction_wins():
    a = _trade("tickformer_v16_pure", 0.50)
    b = _trade("tickformer_v17_sniper", 0.92)  # winner
    c = _trade("tickformer_v18_t180", 0.80)
    out = resolve_mutex_groups([a, b, c])

    actions = {d.strategy_id: d.action for d in out}
    assert actions == {
        "tickformer_v16_pure": "SKIP",
        "tickformer_v17_sniper": "TRADE",
        "tickformer_v18_t180": "SKIP",
    }

    # Winner unchanged.
    winner = next(d for d in out if d.strategy_id == "tickformer_v17_sniper")
    assert winner.confidence_score == 0.92
    assert winner.metadata.get("mutex_group_winner") is None  # untouched

    # Losers carry the lost-reason + winner pointer.
    for sid in ("tickformer_v16_pure", "tickformer_v18_t180"):
        loser = next(d for d in out if d.strategy_id == sid)
        assert loser.skip_reason == "mutex_group_lost"
        assert loser.metadata["mutex_group_winner"] == "tickformer_v17_sniper"
        assert loser.metadata["mutex_group_winner_confidence_score"] == 0.92
        assert loser.metadata["would_direction"] == "UP"


# ── Down-side: symmetric ───────────────────────────────────────────────


def test_down_side_contention_resolves_symmetrically():
    a = _trade("tickformer_v16_pure", 0.88, direction="DOWN")
    b = _trade("tickformer_v18_t180", 0.65, direction="DOWN")
    out = resolve_mutex_groups([a, b])
    actions = {d.strategy_id: d.action for d in out}
    assert actions["tickformer_v16_pure"] == "TRADE"
    assert actions["tickformer_v18_t180"] == "SKIP"
    loser = next(d for d in out if d.strategy_id == "tickformer_v18_t180")
    assert loser.skip_reason == "mutex_group_lost"
    assert loser.metadata["would_direction"] == "DOWN"


# ── Singleton group: no contention, untouched ─────────────────────────


def test_singleton_mutex_group_is_passthrough():
    a = _trade("tickformer_v16_pure", 0.85)
    out = resolve_mutex_groups([a])
    assert out[0].action == "TRADE"
    assert out[0].metadata.get("mutex_group_winner") is None


# ── No mutex_group label → no resolution ──────────────────────────────


def test_decisions_without_mutex_group_pass_through():
    a = _trade("tickformer_v16_pure", 0.85, mutex_group="")
    b = _trade("tickformer_v17_sniper", 0.90, mutex_group="")
    out = resolve_mutex_groups([a, b])
    assert all(d.action == "TRADE" for d in out)


# ── Mixed mutex_groups: independent resolution per label ──────────────


def test_separate_mutex_groups_resolve_independently():
    tf_v16 = _trade("tickformer_v16_pure", 0.70)
    tf_v17 = _trade("tickformer_v17_sniper", 0.88)  # wins tickformer
    other_a = _trade("other_a", 0.50, mutex_group="other_family")
    other_b = _trade("other_b", 0.95, mutex_group="other_family")  # wins other
    out = resolve_mutex_groups([tf_v16, tf_v17, other_a, other_b])

    actions = {d.strategy_id: d.action for d in out}
    assert actions == {
        "tickformer_v16_pure": "SKIP",
        "tickformer_v17_sniper": "TRADE",
        "other_a": "SKIP",
        "other_b": "TRADE",
    }


# ── Self-SKIP (shadow_only) decisions are NOT counted as contenders ───


def test_shadow_only_skip_does_not_block_lone_trade():
    """A SHADOW-mode SKIP carrying mutex_group must not steal the win
    from the only real TRADE in the group."""
    shadow_skip = _skip("tickformer_v16_pure", reason="shadow_only_no_trade")
    real_trade = _trade("tickformer_v17_sniper", 0.92)
    out = resolve_mutex_groups([shadow_skip, real_trade])
    trade = next(d for d in out if d.strategy_id == "tickformer_v17_sniper")
    assert trade.action == "TRADE"
    # SHADOW SKIP unchanged.
    skp = next(d for d in out if d.strategy_id == "tickformer_v16_pure")
    assert skp.skip_reason == "shadow_only_no_trade"


# ── Tie-break determinism: equal scores → strategy_id ascending ───────


def test_tie_break_falls_back_to_strategy_id_ascending():
    a = _trade("tickformer_v18_t180", 0.85)
    b = _trade("tickformer_v17_sniper", 0.85)
    out = resolve_mutex_groups([a, b])
    actions = {d.strategy_id: d.action for d in out}
    # "tickformer_v17_sniper" sorts before "tickformer_v18_t180".
    assert actions["tickformer_v17_sniper"] == "TRADE"
    assert actions["tickformer_v18_t180"] == "SKIP"


# ── Empty input ───────────────────────────────────────────────────────


def test_empty_decisions_returns_empty_list():
    assert resolve_mutex_groups([]) == []
