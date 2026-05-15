"""Hook for v9_1_lgb_only — v9 retrained on priceToBeat-aligned features.

Same architecture as v9_lgb_only except probability source is
`probability_lgb_v9_1` (emitted by timesfm-service when V9_1_ENABLED=true)
rather than `probability_lgb` (v9 PROD candidate_ab83564).

Mirrors the v9_lgb_only delegation pattern: swap probability on the surface,
delegate to v9_ensemble's gate stack, restore.

Hub note #313 — full retrain context.
docs/v9_1_PROVENANCE.md — full lineage.
docs/v9_1_lgb_only_proposal.md — strategy + shadow plan.

Promotion path: shadow A/B vs v9_lgb_only LIVE for ≥7d. Gate criteria in
yaml header. Billy flips LIVE via runtime config (no auto-promote per
post-2026-04-17 rule).

If `probability_lgb_v9_1` is None on the surface (timesfm-service has
V9_1_ENABLED=false or the model failed to load), the hook SKIPs with
reason=`v9_1_model_not_loaded` rather than falling back to v9 PROD —
keeps shadow comparison clean.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9

_STRATEGY_ID = "v9_1_lgb_only"
_VERSION = "9.1.0"


def evaluate_v9_1_lgb_only(surface: "FullDataSurface") -> StrategyDecision:
    """Score with v9.1 (priceToBeat-aligned) booster, same gate stack as v9_lgb_only.

    Surface flow:
    1. Read `probability_lgb_v9_1` from surface (emitted by timesfm-service
       when V9_1_ENABLED=true).
    2. SKIP if None (model not loaded).
    3. Swap onto `probability_lgb` slot.
    4. Force classifier=None (LGB-only mode, same as v9_lgb_only).
    5. Delegate to v9_ensemble gate stack.
    6. Restore surface.
    7. Relabel decision identity as v9_1_lgb_only.
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
    # Force LGB-only fallback path (same as v9_lgb_only)
    object.__setattr__(surface, "probability_classifier", None)
    # Default regime to chop on cold start (same as v9_lgb_only)
    if _orig_regime is None:
        object.__setattr__(surface, "v4_regime", "chop")

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

    # gtc_cap: pull from runtime overrides; default $0.90 (was $0.80 pre-2026-05-14
    # alongside the 4-rung FAK ladder up to $0.92).
    _gtc_cap = _gp.get_float("gtc_cap", None, 0.90)
    meta["gtc_cap"] = _gtc_cap

    return StrategyDecision(
        action=decision.action,
        direction=decision.direction,
        confidence=decision.confidence,
        confidence_score=decision.confidence_score,
        entry_cap=decision.entry_cap,
        collateral_pct=decision.collateral_pct,
        gtc_cap=_gtc_cap,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            decision.entry_reason.replace("v9_ensemble", "v9_1_lgb_only").replace(
                "v9_lgb_only", "v9_1_lgb_only"
            )
            if decision.entry_reason
            else ""
        ),
        skip_reason=decision.skip_reason,
        metadata=meta,
    )
