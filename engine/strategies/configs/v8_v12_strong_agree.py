"""Hook for v8_v12_strong_agree — v8_champion gate stack + strong v9/v12 LGB agreement.

Thesis (Hub note #298, signal-health analysis 2026-05-01):
    v8_champion_lgb_only + v12 strong-agreement gate: 64% -> 98.7% WR
    (n=1,182).

Gate logic (BEFORE delegating to v8_champion_lgb_only's stack):
  1. Read probability_lgb (v9) and probability_lgb_v12. Either None -> SKIP.
  2. Direction agreement: both must be on the same side of 0.5. Else SKIP.
  3. Strong-conviction:
       - UP path:   both p_v9 AND p_v12 >= combo_strong_agree_up_min   (0.65 default)
       - DOWN path: both p_v9 AND p_v12 <= combo_strong_agree_down_max (0.35 default)
     Otherwise SKIP "below_strong_agree_{up|down}_threshold".
  4. On gate pass: swap probability_lgb with the averaged combo probability
     (p_v9 + p_v12) / 2 onto the surface, restore in finally, delegate to
     evaluate_v8_champion_lgb_only. Same swap-and-restore pattern as
     v12_lgb_combo._delegate_with_swapped_lgb.

GHOST mode only. The 98.7% WR claim is from a backward-looking signal-health
study; out-of-sample shadow validation is required before any LIVE flip.
See yaml header for promotion criteria.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp
from strategies.configs.v8_champion_lgb_only import (
    evaluate_v8_champion_lgb_only as _evaluate_v8_lgb,
)

_STRATEGY_ID = "v8_v12_strong_agree"
_VERSION = "1.0.0"

_DEFAULT_UP_MIN = 0.65
_DEFAULT_DOWN_MAX = 0.35


def _combo_strong_agree_up_min() -> float:
    return _gp.get_float(
        "combo_strong_agree_up_min", None, _DEFAULT_UP_MIN
    )


def _combo_strong_agree_down_max() -> float:
    return _gp.get_float(
        "combo_strong_agree_down_max", None, _DEFAULT_DOWN_MAX
    )


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
    """Swap probability_lgb=p_swap onto surface, delegate to v8_champion_lgb_only,
    restore the original surface afterwards. Same swap-and-restore pattern as
    v12_lgb_combo._delegate_with_swapped_lgb but routed through the v8 stack
    (which itself delegates to v9_ensemble with classifier disabled)."""
    _orig_lgb = getattr(surface, "probability_lgb", None)
    object.__setattr__(surface, "probability_lgb", p_swap)
    try:
        decision = _evaluate_v8_lgb(surface)
    finally:
        object.__setattr__(surface, "probability_lgb", _orig_lgb)
    return decision


def evaluate_v8_v12_strong_agree(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Main hook. Strong-agreement gate, then v8_champion_lgb_only stack."""
    p_v9 = getattr(surface, "probability_lgb", None)
    p_v12 = getattr(surface, "probability_lgb_v12", None)

    if p_v9 is None or p_v12 is None:
        return _skip(
            "missing v9 or v12 lgb prob",
            probability_lgb=p_v9,
            probability_lgb_v12=p_v12,
        )

    dir_v9 = "UP" if p_v9 > 0.5 else "DOWN"
    dir_v12 = "UP" if p_v12 > 0.5 else "DOWN"

    if dir_v9 != dir_v12:
        return _skip(
            "v9_v12 direction disagreement",
            probability_lgb=p_v9,
            probability_lgb_v12=p_v12,
            dir_v9=dir_v9,
            dir_v12=dir_v12,
            direction_agree=False,
        )

    direction = dir_v9
    up_min = _combo_strong_agree_up_min()
    down_max = _combo_strong_agree_down_max()

    if direction == "UP":
        if p_v9 < up_min or p_v12 < up_min:
            return _skip(
                "below_strong_agree_up_threshold",
                probability_lgb=p_v9,
                probability_lgb_v12=p_v12,
                dir_v9=dir_v9,
                dir_v12=dir_v12,
                direction_agree=True,
                up_min=up_min,
            )
    else:
        if p_v9 > down_max or p_v12 > down_max:
            return _skip(
                "below_strong_agree_down_threshold",
                probability_lgb=p_v9,
                probability_lgb_v12=p_v12,
                dir_v9=dir_v9,
                dir_v12=dir_v12,
                direction_agree=True,
                down_max=down_max,
            )

    # Strong-agreement gate passed. Swap averaged prob and delegate.
    p_combo = (p_v9 + p_v12) / 2.0
    decision = _delegate_with_swapped_lgb(surface, p_combo)

    meta = dict(decision.metadata or {})
    meta["probability_lgb"] = p_v9
    meta["probability_lgb_v12"] = p_v12
    meta["p_combo"] = round(p_combo, 6)
    meta["dir_v9"] = dir_v9
    meta["dir_v12"] = dir_v12
    meta["direction_agree"] = True
    meta["v8_v12_strong_agree"] = True
    meta["combo_strong_agree_up_min"] = up_min
    meta["combo_strong_agree_down_max"] = down_max

    entry_reason = decision.entry_reason or ""
    if entry_reason:
        entry_reason = entry_reason.replace(
            "v8_champion_lgb_only", _STRATEGY_ID
        ).replace("v9_ensemble", _STRATEGY_ID)

    return StrategyDecision(
        action=decision.action,
        direction=decision.direction,
        confidence=decision.confidence,
        confidence_score=decision.confidence_score,
        entry_cap=decision.entry_cap,
        collateral_pct=decision.collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=entry_reason,
        skip_reason=decision.skip_reason,
        metadata=meta,
    )
