"""Hook for v9_1_cascade_fade_late — v9.1 retrain mirror of v9_cascade_fade_late.

Same gate stack as v9_cascade_fade_late but reads probability_lgb_v9_1
(v9.1 priceToBeat-aligned retrain — Hub note #313) instead of
probability_lgb (v9 PROD candidate_ab83564).

Purpose: direct A/B comparison of v9 vs v9.1 on the walk-forward-validated
CASCADE × DOWN × eval_offset 0-60 (LAST minute) alpha pocket.

Mirrors v9_1_lgb_only.py (PR #466) delegation pattern: swap v9.1 probability
onto the surface's probability_lgb slot, delegate to v9_ensemble's gate
stack, restore.

If `probability_lgb_v9_1` is None on the surface (timesfm-service has
V9_1_ENABLED=false or the model failed to load), the hook SKIPs with
reason=`v9_1_model_not_loaded` rather than falling back to v9 PROD —
keeps shadow comparison clean.

GHOST mode only. Promotion gates in yaml header.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9

_STRATEGY_ID = "v9_1_cascade_fade_late"
_VERSION = "9.1.0-cascade-fade-late"


def evaluate_v9_1_cascade_fade_late(surface: "FullDataSurface") -> StrategyDecision:
    """Score with v9.1 (priceToBeat-aligned) booster on the validated late-window pocket.

    Surface flow:
    1. Read `probability_lgb_v9_1` from surface (emitted by timesfm-service
       when V9_1_ENABLED=true).
    2. SKIP if None (model not loaded — keeps A/B clean, no fallback).
    3. Swap onto `probability_lgb` slot.
    4. Force classifier=None (LGB-only mode, same as v9_1_lgb_only).
    5. Delegate to v9_ensemble gate stack (which reads gate_params from yaml).
    6. Restore surface.
    7. Relabel decision identity as v9_1_cascade_fade_late.
    """
    p_v9_1 = getattr(surface, "probability_lgb_v9_1", None)
    if p_v9_1 is None:
        return StrategyDecision(
            action="SKIP",
            direction=None,
            confidence=None,
            confidence_score=0.0,
            entry_cap=0.0,
            collateral_pct=0.0,
            strategy_id=_STRATEGY_ID,
            strategy_version=_VERSION,
            entry_reason="",
            skip_reason="v9_1_model_not_loaded",
            metadata={"probability_lgb_v9_1": None, "v9_1_enabled": False},
        )

    # Save originals for restoration
    _orig_lgb = getattr(surface, "probability_lgb", None)
    _orig_pc = getattr(surface, "probability_classifier", None)
    _orig_regime = getattr(surface, "v4_regime", None)

    # Swap v9.1 prediction onto probability_lgb slot
    object.__setattr__(surface, "probability_lgb", float(p_v9_1))
    # Force LGB-only fallback path
    object.__setattr__(surface, "probability_classifier", None)
    # Cold-start regime fallback (mirror v9_cascade_fade_late)
    if _orig_regime is None:
        object.__setattr__(surface, "v4_regime", "volatile_trend")

    try:
        decision = _evaluate_v9(surface)
    finally:
        object.__setattr__(surface, "probability_lgb", _orig_lgb)
        object.__setattr__(surface, "probability_classifier", _orig_pc)
        if _orig_regime is None:
            object.__setattr__(surface, "v4_regime", None)

    # Stamp v9.1 metadata for shadow audit
    meta = dict(decision.metadata or {})
    meta["probability_lgb_v9_1"] = float(p_v9_1)
    meta["probability_lgb_prod"] = _orig_lgb
    meta["lgb_only_forced"] = True
    meta["v9_1_active"] = True
    meta["v9_1_cascade_fade_late_specialist"] = True

    entry_reason = decision.entry_reason or ""
    if entry_reason:
        entry_reason = entry_reason.replace(
            "v9_ensemble", _STRATEGY_ID
        ).replace("v8_champion_lgb_only", _STRATEGY_ID).replace(
            "v9_1_lgb_only", _STRATEGY_ID
        )

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
