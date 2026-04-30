"""Hook for v12_lgb_solo — pure v12 LGB strategy (no v9 ensemble blend).

Same gate logic as v9_lgb_only / v10_lgb_only but reads probability_lgb_v12
(the v12 model) instead of probability_lgb (prod v5). Swaps the probability
on the surface before delegating to the v9_ensemble gate stack, then
restores it.

Per Billy's request (hub note #286 — v12 dominance analysis): a pure-v12
strategy is needed for v12 to "stand out" as the basis for future fusion /
v8_champion variants. v12 currently only contributes via v12_lgb_combo's
disagreement-contrarian path (Option C, hub note #299), which gives it
~30% weight in production decisions. v12_lgb_solo measures v12's true
edge alone with no v9 averaging — direct apples-to-apples comparison vs
v9_lgb_only on the same gate stack.

v12 dominance per held-out validation (training data):
  delta 090 (eo 75-105):  v12 62.7% vs v9 59.8% (+2.9pp)
  delta 120 (eo 105-150): v12 61.4% vs v9 56.3% (+5.1pp)
  delta 180 (eo 150-210): v12 65.7% vs v9 58.7% (+7.0pp)
  delta 240 (eo 210-270): v12 63.8% vs v9 55.9% (+7.9pp)

Eval-offset window restricted to 75-240s in YAML — below 75s the v12
manifest (current_v12.json) falls back to v9 prod boosters (no v12
trained model for delta 030/060), so trading those would be redundant
with v9_lgb_only.

Runs as GHOST default until 48h shadow validation completes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9

_STRATEGY_ID = "v12_lgb_solo"
_VERSION = "12.0.0-solo"


def evaluate_v12_lgb_solo(surface: "FullDataSurface") -> StrategyDecision:
    """Delegate to v9_ensemble gate stack using v12 LGB probability.

    Swaps probability_lgb with probability_lgb_v12 on the surface so the
    v8_champion_lgb_only fallback path (reached via v9_ensemble with
    pc=None) uses the v12 model's prediction instead of prod v5.

    Also nulls probability_classifier (same as v9_lgb_only / v10_lgb_only)
    and defaults regime to "chop" on cold start.
    """
    # Read v12 probability — if not available, skip early
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
            metadata={"probability_lgb_v12": None},
        )

    # Swap: put v12 probability into the lgb slot so the gate stack uses it
    _orig_lgb = getattr(surface, "probability_lgb", None)
    object.__setattr__(surface, "probability_lgb", p_v12)

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
    meta["probability_lgb_v12"] = p_v12
    meta["probability_lgb_prod"] = _orig_lgb
    meta["lgb_only_forced"] = True
    meta["v12_solo_model"] = True

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
            decision.entry_reason.replace("v9_ensemble", "v12_lgb_solo").replace(
                "v8_champion_lgb_only", "v12_lgb_solo"
            )
            if decision.entry_reason
            else ""
        ),
        skip_reason=decision.skip_reason,
        metadata=meta,
    )
