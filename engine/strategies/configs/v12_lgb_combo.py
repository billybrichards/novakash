"""Hook for v12_lgb_combo — v9+v12 LGB agreement-required combo strategy.

Trades only when BOTH v9 LGB (probability_lgb) and v12 LGB
(probability_lgb_v12) agree on direction AND the minimum distance from
0.5 exceeds a configurable floor (default 0.10).

When both models agree with sufficient conviction, the combo probability
is the simple average: p_combo = (p_v9 + p_v12) / 2. This averaged
probability is swapped onto the surface before delegating to the
v9_ensemble gate stack (same pattern as v10_lgb_only.py).

v12 model: retrained on window_snapshots with verified Polymarket labels.
3-day replay: +$398 PnL improvement over v9 alone (228 trades). v12
catches v9's big losses while keeping 80% of v9's wins.

Runs as GHOST until human flips to LIVE.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9

_STRATEGY_ID = "v12_lgb_combo"
_VERSION = "12.0.0-combo"

# Default conviction floor — overridden by gate_params.combo_min_dist in YAML
_DEFAULT_COMBO_MIN_DIST = 0.10


def evaluate_v12_lgb_combo(surface: "FullDataSurface") -> StrategyDecision:
    """Delegate to v9_ensemble gate stack using combo v9+v12 LGB probability.

    Decision logic:
    1. Read p_v9 (probability_lgb) and p_v12 (probability_lgb_v12).
    2. If either is None -> SKIP.
    3. Both must agree on direction (UP/DOWN).
    4. min(dist_v9, dist_v12) must exceed combo_min_dist floor.
    5. Average the two probabilities and delegate to v9_ensemble.
    """
    # ── Read both probabilities ──────────────────────────────────────────
    p_v9 = getattr(surface, "probability_lgb", None)
    p_v12 = getattr(surface, "probability_lgb_v12", None)

    if p_v12 is None:
        return StrategyDecision(
            action="SKIP",
            direction=None,
            confidence=None,
            confidence_score=0.0,
            entry_cap=None,
            collateral_pct=None,
            strategy_id=_STRATEGY_ID,
            strategy_version=_VERSION,
            entry_reason="",
            skip_reason="probability_lgb_v12 unavailable",
            metadata={"probability_lgb_v12": None, "probability_lgb": p_v9},
        )

    if p_v9 is None:
        return StrategyDecision(
            action="SKIP",
            direction=None,
            confidence=None,
            confidence_score=0.0,
            entry_cap=None,
            collateral_pct=None,
            strategy_id=_STRATEGY_ID,
            strategy_version=_VERSION,
            entry_reason="",
            skip_reason="probability_lgb unavailable",
            metadata={"probability_lgb_v12": p_v12, "probability_lgb": None},
        )

    # ── Direction agreement check ────────────────────────────────────────
    dir_v9 = "UP" if p_v9 > 0.5 else "DOWN"
    dir_v12 = "UP" if p_v12 > 0.5 else "DOWN"
    direction_agree = dir_v9 == dir_v12

    if not direction_agree:
        return StrategyDecision(
            action="SKIP",
            direction=None,
            confidence=None,
            confidence_score=0.0,
            entry_cap=None,
            collateral_pct=None,
            strategy_id=_STRATEGY_ID,
            strategy_version=_VERSION,
            entry_reason="",
            skip_reason="v9_v12_direction_disagreement",
            metadata={
                "probability_lgb": p_v9,
                "probability_lgb_v12": p_v12,
                "dir_v9": dir_v9,
                "dir_v12": dir_v12,
                "direction_agree": False,
            },
        )

    # ── Conviction floor check ───────────────────────────────────────────
    dist_v9 = abs(p_v9 - 0.5)
    dist_v12 = abs(p_v12 - 0.5)
    combo_dist = min(dist_v9, dist_v12)

    if combo_dist < _DEFAULT_COMBO_MIN_DIST:
        return StrategyDecision(
            action="SKIP",
            direction=None,
            confidence=None,
            confidence_score=0.0,
            entry_cap=None,
            collateral_pct=None,
            strategy_id=_STRATEGY_ID,
            strategy_version=_VERSION,
            entry_reason="",
            skip_reason="combo_below_conviction_floor",
            metadata={
                "probability_lgb": p_v9,
                "probability_lgb_v12": p_v12,
                "dist_v9": round(dist_v9, 4),
                "dist_v12": round(dist_v12, 4),
                "combo_dist": round(combo_dist, 4),
                "combo_min_dist": _DEFAULT_COMBO_MIN_DIST,
                "direction_agree": True,
            },
        )

    # ── Compute combo probability and delegate ───────────────────────────
    p_combo = (p_v9 + p_v12) / 2.0

    # Swap: put combo probability into the lgb slot so the gate stack uses it
    _orig_lgb = getattr(surface, "probability_lgb", None)
    object.__setattr__(surface, "probability_lgb", p_combo)

    # Force LGB-only: null out classifier (same as v9_lgb_only / v10_lgb_only)
    _orig_pc = getattr(surface, "probability_classifier", None)
    object.__setattr__(surface, "probability_classifier", None)

    # Cold start regime bypass
    _orig_regime = getattr(surface, "v4_regime", None)
    if _orig_regime is None:
        object.__setattr__(surface, "v4_regime", "chop")

    try:
        decision = _evaluate_v9(surface)
    finally:
        # Restore all swapped fields
        object.__setattr__(surface, "probability_lgb", _orig_lgb)
        object.__setattr__(surface, "probability_classifier", _orig_pc)
        if _orig_regime is None:
            object.__setattr__(surface, "v4_regime", None)

    # ── Build enriched metadata ──────────────────────────────────────────
    meta = dict(decision.metadata or {})
    meta["probability_classifier"] = None
    meta["probability_lgb"] = p_v9
    meta["probability_lgb_v12"] = p_v12
    meta["p_combo"] = round(p_combo, 6)
    meta["dist_v9"] = round(dist_v9, 4)
    meta["dist_v12"] = round(dist_v12, 4)
    meta["combo_dist"] = round(combo_dist, 4)
    meta["direction_agree"] = True
    meta["lgb_only_forced"] = True
    meta["v12_combo_model"] = True

    return StrategyDecision(
        action=decision.action,
        direction=decision.direction,
        confidence=decision.confidence,
        confidence_score=min(combo_dist * 2, 1.0),
        entry_cap=decision.entry_cap,
        collateral_pct=decision.collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            decision.entry_reason.replace("v9_ensemble", "v12_lgb_combo").replace(
                "v8_champion_lgb_only", "v12_lgb_combo"
            )
            if decision.entry_reason
            else ""
        ),
        skip_reason=decision.skip_reason,
        metadata=meta,
    )
