"""Hook for v10_up_late_window — v10 LGB UP specialist for late mid-window.

Thesis (Hub note #304 — real-trades 6d):
    v10 LGB at TRANSITION × UP × T-61-120 × k=3
    = 75% WR n=50 +$53 over ~6 days REAL trades.

Same swap-and-restore pattern as v10_lgb_only.py — reads probability_lgb_v10
and slots it into the probability_lgb position so the v9_ensemble gate stack
uses v10's prediction.

Specialisation lives in the YAML gate_params:

  * down_min_fill_price=1.01 — HARD UP-only lock (NOT VHC-bypassable per
    v9_ensemble.py:1173-1196).
  * block_up_vpin_regimes=[CASCADE] — anti-cell guard (UP×CASCADE = -$71).
  * tradeable_v4_regimes=[chop, volatile_trend] — proven cells only.
  * min_offset_sec=60, max_offset_sec=120 — late mid-window sweet spot.
  * min_consecutive_pass_ticks=3 — TRANSITION stability filter.

GHOST mode only.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9

_STRATEGY_ID = "v10_up_late_window"
_VERSION = "1.0.0-ghost"


def evaluate_v10_up_late_window(surface: "FullDataSurface") -> StrategyDecision:
    """Delegate to v9_ensemble using v10 LGB probability.

    Swap probability_lgb with probability_lgb_v10, null classifier (LGB-only),
    delegate, then restore. Mirrors v10_lgb_only.py.
    """
    p_v10 = getattr(surface, "probability_lgb_v10", None)
    if p_v10 is None:
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
            skip_reason="probability_lgb_v10 unavailable",
            metadata={"probability_lgb_v10": None},
        )

    _orig_lgb = getattr(surface, "probability_lgb", None)
    object.__setattr__(surface, "probability_lgb", p_v10)

    _orig_pc = getattr(surface, "probability_classifier", None)
    object.__setattr__(surface, "probability_classifier", None)

    _orig_regime = getattr(surface, "v4_regime", None)
    if _orig_regime is None:
        object.__setattr__(surface, "v4_regime", "chop")

    try:
        decision = _evaluate_v9(surface)
    finally:
        object.__setattr__(surface, "probability_lgb", _orig_lgb)
        object.__setattr__(surface, "probability_classifier", _orig_pc)
        if _orig_regime is None:
            object.__setattr__(surface, "v4_regime", None)

    meta = dict(decision.metadata or {})
    meta["probability_classifier"] = None
    meta["probability_lgb_v10"] = p_v10
    meta["probability_lgb_prod"] = _orig_lgb
    meta["lgb_only_forced"] = True
    meta["v10_up_late_window_specialist"] = True

    entry_reason = decision.entry_reason or ""
    if entry_reason:
        entry_reason = entry_reason.replace(
            "v9_ensemble", _STRATEGY_ID
        ).replace("v8_champion_lgb_only", _STRATEGY_ID)

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
