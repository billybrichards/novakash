"""Hook for v10_lgb_only — v10 LGB model shadow strategy.

Same gate logic as v9_lgb_only but reads probability_lgb_v10 (the v10 model)
instead of probability_lgb (prod v5). Swaps the probability on the surface
before delegating to the v9_ensemble gate stack, then restores it.

v10 model: 112 features, Optuna-tuned, 76.0% acc vs prod 70.6% on identical
test data. Trained TRADE-only on 157k rows with CoinGlass, TimesFM quantiles,
and engineered interaction features.

Runs as GHOST alongside v9_lgb_only (LIVE) for A/B comparison.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9

_STRATEGY_ID = "v10_lgb_only"
_VERSION = "10.0.0-lgb"


def evaluate_v10_lgb_only(surface: "FullDataSurface") -> StrategyDecision:
    """Delegate to v9_ensemble gate stack using v10 LGB probability.

    Swaps probability_lgb with probability_lgb_v10 on the surface so the
    v8_champion_lgb_only fallback path (reached via v9_ensemble with
    pc=None) uses the v10 model's prediction instead of prod v5.

    Also nulls probability_classifier (same as v9_lgb_only) and defaults
    regime to "chop" on cold start.
    """
    # Read v10 probability — if not available, skip early
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

    # Swap: put v10 probability into the lgb slot so the gate stack uses it
    _orig_lgb = getattr(surface, "probability_lgb", None)
    object.__setattr__(surface, "probability_lgb", p_v10)

    # Force LGB-only: null out classifier
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

    meta = dict(decision.metadata or {})
    meta["probability_classifier"] = None
    meta["probability_lgb_v10"] = p_v10
    meta["probability_lgb_prod"] = _orig_lgb
    meta["lgb_only_forced"] = True
    meta["v10_model"] = True

    return StrategyDecision(
        action=decision.action,
        direction=decision.direction,
        confidence=decision.confidence,
        confidence_score=decision.confidence_score,
        entry_cap=decision.entry_cap,
        collateral_pct=decision.collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            decision.entry_reason.replace("v9_ensemble", "v10_lgb_only").replace(
                "v8_champion_lgb_only", "v10_lgb_only"
            )
            if decision.entry_reason
            else ""
        ),
        skip_reason=decision.skip_reason,
        metadata=meta,
    )
