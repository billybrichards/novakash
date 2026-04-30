"""Hook for v9_lgb_only — LGB-only fork of v9_ensemble.

Identical gate logic to v9_ensemble but forces the LGB-only fallback path
(classifier disabled). All v9 features preserved: 3-tick confirmation, delta
gate, exit monitoring, VHC bypasses.

Created 2026-04-25: v9_ensemble was performing well in LGB-only mode while
the classifier was being rebuilt. This fork preserves that proven config as a
standalone strategy so both can run in parallel once the classifier is ready.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9

_STRATEGY_ID = "v9_lgb_only"
_VERSION = "9.1.0-lgb"


def evaluate_v9_lgb_only(surface: "FullDataSurface") -> StrategyDecision:
    """Delegate to v9_ensemble gate stack; relabel with v9_lgb_only identity.

    Forces LGB-only mode by nulling probability_classifier on the surface
    before delegating. This ensures v9_ensemble takes the fallback-to-
    v8_lgb_only path regardless of whether the classifier box is serving.

    With pc=None, v9_ensemble gate R1 immediately delegates to
    evaluate_v8_champion_lgb_only (zero classifier code paths reached).
    Even if fallback_to_lgb_on_pc_null were overridden to False, the
    ensemble path would TypeError on abs(None - pl) and emit ERROR
    (crash > wrong trade).
    """
    # Force LGB-only: null out classifier so v9_ensemble takes fallback path.
    # Uses object.__setattr__ because FullDataSurface is a frozen dataclass.
    _orig_pc = getattr(surface, "probability_classifier", None)
    object.__setattr__(surface, "probability_classifier", None)

    # When regime is None (cold start / TimesFM warming up), default to
    # "chop" so the regime gate passes. LGB doesn't need HMM regime to
    # make predictions — it's just an extra filter. Only v9_lgb_only
    # gets this bypass; v9_ensemble still requires regime.
    _orig_regime = getattr(surface, "v4_regime", None)
    if _orig_regime is None:
        object.__setattr__(surface, "v4_regime", "chop")

    try:
        decision = _evaluate_v9(surface)
    finally:
        object.__setattr__(surface, "probability_classifier", _orig_pc)
        if _orig_regime is None:
            object.__setattr__(surface, "v4_regime", None)

    # Fix audit: stamp pc=None in metadata so decision logs correctly
    # reflect that classifier was NOT used (even though surface had it)
    meta = dict(decision.metadata or {})
    meta["probability_classifier"] = None
    meta["lgb_only_forced"] = True
    meta["probability_lgb_prod"] = surface.probability_lgb  # mirror v9 conviction for audit/filter parity with v10_lgb_only

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
            decision.entry_reason.replace("v9_ensemble", "v9_lgb_only")
            if decision.entry_reason
            else ""
        ),
        skip_reason=decision.skip_reason,
        metadata=meta,
    )
