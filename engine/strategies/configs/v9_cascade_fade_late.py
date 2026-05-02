"""Hook for v9_cascade_fade_late — CASCADE×DOWN fade specialist at LAST minute of window.

Thesis (Hub notes #314 + #315 — walk-forward 4-fold CV across 30d real trades):
    v9 LGB at CASCADE × DOWN × eval_offset 0-60 (LAST minute) × dist[0.10+] × k=1
    = +$32 n=88 82.8% WR. Walk-forward 3/4 folds passed.

Corrects v9_cascade_fade_early (deployed at eval_offset 181-240 / FIRST minute,
which failed walk-forward — that pocket was an 87h artifact, not robust to
30d cyclical regimes).

This hook delegates to v9_ensemble.evaluate_v9_ensemble using v9 PROD's LGB
probability (probability_lgb), forcing LGB-only mode (probability_classifier
= None) — same swap-and-restore pattern as v10_lgb_only.py and v9_cascade_fade_early.py.

The actual specialisation lives in the YAML gate_params:

  * up_min_fill_price=1.01 — HARD DOWN-only lock (NOT VHC-bypassable per
    v9_ensemble.py:1121-1196).
  * block_down_vpin_regimes excludes everything but CASCADE for DOWN.
  * lgb_dist_min_down=0.10 catches the alpha pocket (LOW conviction).
  * min_consecutive_pass_ticks=1 — cascade alpha decays fast.
  * min_offset_sec=24, max_offset_sec=60 — LAST MINUTE of window (validated).
  * VHC bypass paths all disabled.

GHOST mode only. Promotion gate: see yaml header.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9

_STRATEGY_ID = "v9_cascade_fade_late"
_VERSION = "9.0.0-cascade-fade-late"


def evaluate_v9_cascade_fade_late(surface: "FullDataSurface") -> StrategyDecision:
    """Delegate to v9_ensemble gate stack with v9 PROD LGB probability.

    Same swap-and-restore pattern as v10_lgb_only / v9_cascade_fade_early.
    We do NOT swap a different model in — we use v9's prod LGB
    (probability_lgb). The specialisation comes entirely from the
    gate_params (DOWN-only lock, CASCADE-only regime, k=1, LAST minute
    of window, low-conviction floor).
    """
    p_v9 = getattr(surface, "probability_lgb", None)
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
            metadata={"probability_lgb": None},
        )

    # Force LGB-only path: null the classifier so v9_ensemble takes the
    # v8_champion_lgb_only fallback.
    _orig_pc = getattr(surface, "probability_classifier", None)
    object.__setattr__(surface, "probability_classifier", None)

    # Cold-start regime fallback. If v4_regime is None, the v4_regime gate
    # would skip — give it a tradeable default from our allowlist.
    _orig_regime = getattr(surface, "v4_regime", None)
    if _orig_regime is None:
        object.__setattr__(surface, "v4_regime", "volatile_trend")

    try:
        decision = _evaluate_v9(surface)
    finally:
        object.__setattr__(surface, "probability_classifier", _orig_pc)
        if _orig_regime is None:
            object.__setattr__(surface, "v4_regime", None)

    meta = dict(decision.metadata or {})
    meta["probability_classifier"] = None
    meta["probability_lgb"] = p_v9
    meta["lgb_only_forced"] = True
    meta["v9_cascade_fade_late_specialist"] = True

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
