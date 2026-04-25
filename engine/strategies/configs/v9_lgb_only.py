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

    Safety: also force-overrides fallback_to_lgb_on_pc_null via gate_params
    so even a runtime DB override cannot re-enable the classifier path.
    """
    # Force LGB-only: null out classifier so v9_ensemble takes fallback path.
    # Belt-and-suspenders: also force the fallback flag via gate_params so
    # a runtime override of fallback_to_lgb_on_pc_null=false cannot break
    # isolation. Without this, a bad override + race could re-enable classifier.
    _orig_pc = getattr(surface, "probability_classifier", None)
    surface.probability_classifier = None

    # Force fallback flag in case runtime override tries to disable it
    from strategies import gate_params as _gp_mod
    _active_params = getattr(_gp_mod, "_ACTIVE", None)
    _orig_fallback = None
    if _active_params is not None and hasattr(_active_params, "get"):
        _orig_fallback = _active_params.get("fallback_to_lgb_on_pc_null")
        _active_params["fallback_to_lgb_on_pc_null"] = True

    try:
        decision = _evaluate_v9(surface)
    finally:
        # Restore so other strategies sharing the surface still see pc
        surface.probability_classifier = _orig_pc
        if _active_params is not None and _orig_fallback is not None:
            _active_params["fallback_to_lgb_on_pc_null"] = _orig_fallback

    # Fix audit: stamp pc=None in metadata so decision logs correctly
    # reflect that classifier was NOT used (even though surface had it)
    meta = dict(decision.metadata or {})
    meta["probability_classifier"] = None
    meta["lgb_only_forced"] = True

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
