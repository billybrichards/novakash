"""Cross-strategy mutex-group resolver (PR #619 review, FIX 2).

When multiple strategies in the same mutex_group fire TRADE on the
same window evaluation, only the highest-conviction one survives —
the rest are demoted to SKIP with ``skip_reason=mutex_group_lost``.
Prevents the tickformer_v16/v17/v18 family (and any future cohort
declaring a shared ``gate_params.mutex_group`` label) from stacking
exposure or competing for the same fill.

Mechanism: engine-side post-processor invoked by
``StrategyRegistry.evaluate_all`` AFTER every strategy hook has
returned its decision for the window. Choosing engine-side over
strategy-side because (a) there is no existing "shared pending
decisions" shelf in the codebase, (b) it keeps the resolver authoritative
and decision-attempt logging (strategy_decisions table) sees the
demoted SKIP correctly, and (c) the strategies remain pure functions
of their surface.

The "highest conviction" tie-break is:

    1. ``confidence_score`` descending (raw probability distance).
    2. ``strategy_id`` ascending (deterministic fallback).

Future extension: per-strategy weight from gate_params
``mutex_group_priority`` (int) would slot in between (1) and (2).
"""

from __future__ import annotations

from dataclasses import replace as _dc_replace
from typing import Iterable

from domain.value_objects import StrategyDecision


_LOST_REASON = "mutex_group_lost"


def _mutex_label(decision: StrategyDecision) -> str:
    """Return the mutex_group label from decision.metadata, or "".

    Lookup is metadata-only — the base hook is responsible for
    propagating ``gate_params.mutex_group`` into decision.metadata.
    """
    meta = decision.metadata or {}
    label = meta.get("mutex_group")
    if not isinstance(label, str):
        return ""
    return label.strip()


def _sort_key(decision: StrategyDecision):
    """Higher conviction wins. Deterministic tiebreaker by strategy_id.

    Returns a tuple sortable in DESCENDING priority order via
    ``reverse=True`` on the ``confidence_score`` axis only.
    """
    score = decision.confidence_score or 0.0
    return (-float(score), decision.strategy_id or "")


def resolve_mutex_groups(
    decisions: Iterable[StrategyDecision],
) -> list[StrategyDecision]:
    """Demote mutex-group losers to SKIP.

    Operates only on decisions whose ``action == "TRADE"`` and whose
    metadata carries a non-empty ``mutex_group`` label. SKIP / ERROR
    decisions are passed through untouched (a losing TRADE that has
    already self-SKIPped via ``shadow_only_no_trade`` carries
    ``mutex_group`` in metadata but ``action=SKIP``, so it is also
    passed through — the resolver only kicks in once a strategy
    actually wants to fire).

    Args:
        decisions: full list of decisions from this window evaluation.

    Returns:
        A new list in the original order, with losers replaced by
        SKIP-with-decision-record. Winners are unchanged.
    """
    decisions = list(decisions)
    if not decisions:
        return decisions

    # Bucket TRADE decisions by mutex_group label.
    groups: dict[str, list[int]] = {}
    for idx, dec in enumerate(decisions):
        if dec.action != "TRADE":
            continue
        label = _mutex_label(dec)
        if not label:
            continue
        groups.setdefault(label, []).append(idx)

    if not groups:
        return decisions

    out = list(decisions)
    for label, idxs in groups.items():
        if len(idxs) <= 1:
            continue  # singleton group — no contention
        contenders = [(i, decisions[i]) for i in idxs]
        contenders.sort(key=lambda pair: _sort_key(pair[1]))
        winner_idx = contenders[0][0]
        winner = decisions[winner_idx]
        for loser_idx, loser in contenders[1:]:
            loser_meta = dict(loser.metadata or {})
            loser_meta.update(
                {
                    "mutex_group": label,
                    "mutex_group_winner": winner.strategy_id,
                    "mutex_group_winner_confidence_score": (
                        winner.confidence_score or 0.0
                    ),
                    "mutex_group_loser_confidence_score": (
                        loser.confidence_score or 0.0
                    ),
                    "would_trade": True,
                    "would_direction": loser.direction,
                }
            )
            out[loser_idx] = _dc_replace(
                loser,
                action="SKIP",
                direction=None,
                confidence=None,
                confidence_score=0.0,
                entry_cap=0.0,
                collateral_pct=0.0,
                gtc_cap=None,
                entry_reason="",
                skip_reason=_LOST_REASON,
                metadata=loser_meta,
            )
    return out
