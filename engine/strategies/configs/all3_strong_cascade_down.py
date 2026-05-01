"""Hook for all3_strong_cascade_down — v9+v10+v12 triple-agreement CASCADE×DOWN.

Thesis (Hub note #307 — exploratory multi-signal, 87h shadow):
    v9 + v10 + v12 ALL agree direction × CASCADE × DOWN × any T-band × k=1
    = 84.6% WR n=52 +$40.08.

Pattern follows v8_v12_strong_agree.py (2-signal) extended to 3 signals.
Reads probability_lgb (v9), probability_lgb_v10, probability_lgb_v12. ALL
must be:
  1. Available (any None → SKIP)
  2. Same direction (any disagree → SKIP)
  3. dist >= combo_min_dist_{v9,v10,v12} (any below → SKIP)

On pass: averages the 3 probabilities, swaps onto surface, delegates to
v9_ensemble's gate stack (LGB-only via classifier=None). Same swap-and-restore
pattern as v8_v12_strong_agree.py and v12_lgb_combo.py.

The DOWN-only lock + CASCADE-only regime + k=1 + low-conviction floor live
entirely in the YAML gate_params. The hook's job is the 3-way agreement gate.

GHOST mode only.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9

_STRATEGY_ID = "all3_strong_cascade_down"
_VERSION = "1.0.0-ghost"

_DEFAULT_MIN_DIST = 0.10


def _combo_min_dist_v9() -> float:
    return _gp.get_float("combo_min_dist_v9", None, _DEFAULT_MIN_DIST)


def _combo_min_dist_v10() -> float:
    return _gp.get_float("combo_min_dist_v10", None, _DEFAULT_MIN_DIST)


def _combo_min_dist_v12() -> float:
    return _gp.get_float("combo_min_dist_v12", None, _DEFAULT_MIN_DIST)


def _skip(reason: str, **meta) -> StrategyDecision:
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
        skip_reason=reason,
        metadata=meta,
    )


def _delegate_with_swapped_lgb(
    surface: "FullDataSurface", p_swap: float
) -> StrategyDecision:
    """Swap probability_lgb=p_swap onto surface, force LGB-only stack,
    delegate to v9_ensemble, restore the surface afterwards.

    Mirrors v12_lgb_combo._delegate_with_swapped_lgb.
    """
    _orig_lgb = getattr(surface, "probability_lgb", None)
    object.__setattr__(surface, "probability_lgb", p_swap)

    _orig_pc = getattr(surface, "probability_classifier", None)
    object.__setattr__(surface, "probability_classifier", None)

    _orig_regime = getattr(surface, "v4_regime", None)
    if _orig_regime is None:
        object.__setattr__(surface, "v4_regime", "volatile_trend")

    try:
        decision = _evaluate_v9(surface)
    finally:
        object.__setattr__(surface, "probability_lgb", _orig_lgb)
        object.__setattr__(surface, "probability_classifier", _orig_pc)
        if _orig_regime is None:
            object.__setattr__(surface, "v4_regime", None)

    return decision


def evaluate_all3_strong_cascade_down(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Triple-agreement gate, then delegate to v9_ensemble."""
    p_v9 = getattr(surface, "probability_lgb", None)
    p_v10 = getattr(surface, "probability_lgb_v10", None)
    p_v12 = getattr(surface, "probability_lgb_v12", None)

    if p_v9 is None or p_v10 is None or p_v12 is None:
        return _skip(
            "missing_lgb_signal",
            probability_lgb=p_v9,
            probability_lgb_v10=p_v10,
            probability_lgb_v12=p_v12,
        )

    dir_v9 = "UP" if p_v9 > 0.5 else "DOWN"
    dir_v10 = "UP" if p_v10 > 0.5 else "DOWN"
    dir_v12 = "UP" if p_v12 > 0.5 else "DOWN"

    # All three must agree direction
    if not (dir_v9 == dir_v10 == dir_v12):
        return _skip(
            "v9_v10_v12_direction_disagreement",
            probability_lgb=p_v9,
            probability_lgb_v10=p_v10,
            probability_lgb_v12=p_v12,
            dir_v9=dir_v9,
            dir_v10=dir_v10,
            dir_v12=dir_v12,
            direction_agree=False,
        )

    # Conviction floor on each model
    dist_v9 = abs(p_v9 - 0.5)
    dist_v10 = abs(p_v10 - 0.5)
    dist_v12 = abs(p_v12 - 0.5)

    min_dist_v9 = _combo_min_dist_v9()
    min_dist_v10 = _combo_min_dist_v10()
    min_dist_v12 = _combo_min_dist_v12()

    base_meta = {
        "probability_lgb": p_v9,
        "probability_lgb_v10": p_v10,
        "probability_lgb_v12": p_v12,
        "dir_v9": dir_v9,
        "dir_v10": dir_v10,
        "dir_v12": dir_v12,
        "dist_v9": round(dist_v9, 4),
        "dist_v10": round(dist_v10, 4),
        "dist_v12": round(dist_v12, 4),
        "direction_agree": True,
    }

    if dist_v9 < min_dist_v9:
        return _skip(
            "v9_below_combo_min_dist",
            **base_meta,
            combo_min_dist_v9=min_dist_v9,
        )
    if dist_v10 < min_dist_v10:
        return _skip(
            "v10_below_combo_min_dist",
            **base_meta,
            combo_min_dist_v10=min_dist_v10,
        )
    if dist_v12 < min_dist_v12:
        return _skip(
            "v12_below_combo_min_dist",
            **base_meta,
            combo_min_dist_v12=min_dist_v12,
        )

    # Triple-agreement passed. Average and delegate.
    p_combo = (p_v9 + p_v10 + p_v12) / 3.0
    decision = _delegate_with_swapped_lgb(surface, p_combo)

    combo_min_dist = min(dist_v9, dist_v10, dist_v12)

    meta = dict(decision.metadata or {})
    meta.update(base_meta)
    meta["p_combo"] = round(p_combo, 6)
    meta["combo_min_dist"] = round(combo_min_dist, 4)
    meta["lgb_only_forced"] = True
    meta["all3_strong_cascade_down"] = True

    entry_reason = decision.entry_reason or ""
    if entry_reason:
        entry_reason = entry_reason.replace(
            "v9_ensemble", _STRATEGY_ID
        ).replace("v8_champion_lgb_only", _STRATEGY_ID)

    return StrategyDecision(
        action=decision.action,
        direction=decision.direction,
        confidence=decision.confidence,
        # confidence_score uses tightest model's distance (most cautious)
        confidence_score=min(combo_min_dist * 2, 1.0),
        entry_cap=decision.entry_cap,
        collateral_pct=decision.collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=entry_reason,
        skip_reason=decision.skip_reason,
        metadata=meta,
    )
