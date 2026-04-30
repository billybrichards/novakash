"""Hook for v12_lgb_combo — v9+v12 LGB combo strategy with disagreement-contrarian path.

Implements two trading modes:

* **Option A (agreement path)**: when v9 LGB (`probability_lgb`) and v12 LGB
  (`probability_lgb_v12`) agree on direction AND `min(dist_v9, dist_v12) >=
  combo_min_dist`, trade the averaged probability `(p_v9 + p_v12) / 2`.
  Sizing via existing CLOBSizingGate (max 2.5x kelly — same as v10 baseline).

* **Option C (disagreement-contrarian path)**: when v9 + v12 disagree direction
  AND `dist_v12 >= v12_contrarian_min_dist`, trade in **v12's direction** at
  **0.5x kelly** (collateral_pct halved). Hub note #299 + 3-day replay shows
  v12 is right 51.5% of the time on disagreements with a +$398/3d edge from
  asymmetry (v12-fix avg|pnl|=$12 vs v12-break $6.7). Half-stake bounds blast
  radius if the contrarian edge fades.

In both paths the hook swaps probabilities onto the surface before delegating
to the v9_ensemble gate stack (same delegation pattern as v10_lgb_only.py),
and restores the original surface afterwards.

v12 model: retrained on window_snapshots with verified Polymarket labels.
3-day replay: +$398 PnL improvement over v9 alone (228 trades). v12 catches
v9's big losses while keeping 80% of v9's wins.

Runs as GHOST until human flips to LIVE.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9

_STRATEGY_ID = "v12_lgb_combo"
_VERSION = "12.0.0-combo"

# Default conviction floors — overridden by gate_params in YAML / contextvar.
_DEFAULT_COMBO_MIN_DIST = 0.10
_DEFAULT_V12_CONTRARIAN_MIN_DIST = 0.10
_DEFAULT_V12_CONTRARIAN_KELLY_MOD = 0.5
_DEFAULT_V12_CONTRARIAN_ENABLED = True


def _combo_min_dist() -> float:
    return _gp.get_float("combo_min_dist", None, _DEFAULT_COMBO_MIN_DIST)


def _v12_contrarian_enabled() -> bool:
    return _gp.get_bool(
        "v12_contrarian_enabled", None, _DEFAULT_V12_CONTRARIAN_ENABLED
    )


def _v12_contrarian_min_dist() -> float:
    return _gp.get_float(
        "v12_contrarian_min_dist", None, _DEFAULT_V12_CONTRARIAN_MIN_DIST
    )


def _v12_contrarian_kelly_modifier() -> float:
    return _gp.get_float(
        "v12_contrarian_kelly_modifier", None, _DEFAULT_V12_CONTRARIAN_KELLY_MOD
    )


def _delegate_with_swapped_lgb(
    surface: "FullDataSurface", p_swap: float
) -> StrategyDecision:
    """Swap probability_lgb=p_swap on surface, force LGB-only stack, delegate
    to v9_ensemble, then restore the surface. Returns the raw v9_ensemble
    decision (caller is responsible for rebuilding identity / metadata)."""
    _orig_lgb = getattr(surface, "probability_lgb", None)
    object.__setattr__(surface, "probability_lgb", p_swap)

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

    return decision


def evaluate_v12_lgb_combo(surface: "FullDataSurface") -> StrategyDecision:
    """Evaluate v12_lgb_combo (Options A + C).

    Decision logic:
    1. Read p_v9 (probability_lgb) and p_v12 (probability_lgb_v12).
    2. If either is None → SKIP.
    3. **Agreement path (A)**: if both directions match AND
       min(dist_v9, dist_v12) >= combo_min_dist, average probabilities
       and delegate to v9_ensemble at full kelly.
    4. **Disagreement path (C)**: if directions differ AND
       dist_v12 >= v12_contrarian_min_dist AND v12_contrarian_enabled,
       trust v12's call, delegate to v9_ensemble using p_v12, then
       halve the returned collateral_pct (0.5x kelly).
    5. Otherwise SKIP with an explicit reason.
    """
    # ── Read both probabilities ──────────────────────────────────────────
    p_v9 = getattr(surface, "probability_lgb", None)
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
            metadata={"probability_lgb_v12": None, "probability_lgb": p_v9},
        )

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
            metadata={"probability_lgb_v12": p_v12, "probability_lgb": None},
        )

    # ── Direction split ──────────────────────────────────────────────────
    dir_v9 = "UP" if p_v9 > 0.5 else "DOWN"
    dir_v12 = "UP" if p_v12 > 0.5 else "DOWN"
    dist_v9 = abs(p_v9 - 0.5)
    dist_v12 = abs(p_v12 - 0.5)

    if dir_v9 == dir_v12:
        return _evaluate_agreement_path(
            surface, p_v9, p_v12, dist_v9, dist_v12, dir_v9
        )

    # Directions disagree — Option C contrarian path.
    return _evaluate_disagreement_path(
        surface, p_v9, p_v12, dist_v9, dist_v12, dir_v9, dir_v12
    )


def _evaluate_agreement_path(
    surface: "FullDataSurface",
    p_v9: float,
    p_v12: float,
    dist_v9: float,
    dist_v12: float,
    direction: str,
) -> StrategyDecision:
    """Option A — both models agree direction. Trade averaged probability."""
    combo_dist = min(dist_v9, dist_v12)
    combo_min_dist = _combo_min_dist()

    if combo_dist < combo_min_dist:
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
            skip_reason="combo_below_conviction_floor",
            metadata={
                "probability_lgb": p_v9,
                "probability_lgb_v12": p_v12,
                "dist_v9": round(dist_v9, 4),
                "dist_v12": round(dist_v12, 4),
                "combo_dist": round(combo_dist, 4),
                "combo_min_dist": combo_min_dist,
                "direction_agree": True,
                "v12_contrarian_mode": False,
            },
        )

    p_combo = (p_v9 + p_v12) / 2.0
    decision = _delegate_with_swapped_lgb(surface, p_combo)

    meta = dict(decision.metadata or {})
    meta["probability_classifier"] = None
    meta["probability_lgb"] = p_v9
    meta["probability_lgb_v12"] = p_v12
    meta["p_combo"] = round(p_combo, 6)
    meta["dist_v9"] = round(dist_v9, 4)
    meta["dist_v12"] = round(dist_v12, 4)
    meta["combo_dist"] = round(combo_dist, 4)
    meta["direction_agree"] = True
    meta["lgb_only_forced"] = True
    meta["v12_combo_model"] = True
    meta["v12_contrarian_mode"] = False

    return StrategyDecision(
        action=decision.action,
        direction=decision.direction,
        confidence=decision.confidence,
        confidence_score=min(combo_dist * 2, 1.0),
        entry_cap=decision.entry_cap,
        collateral_pct=decision.collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            decision.entry_reason.replace("v9_ensemble", "v12_lgb_combo").replace(
                "v8_champion_lgb_only", "v12_lgb_combo"
            )
            if decision.entry_reason
            else ""
        ),
        skip_reason=decision.skip_reason,
        metadata=meta,
    )


def _evaluate_disagreement_path(
    surface: "FullDataSurface",
    p_v9: float,
    p_v12: float,
    dist_v9: float,
    dist_v12: float,
    dir_v9: str,
    dir_v12: str,
) -> StrategyDecision:
    """Option C — v9 and v12 disagree direction. Trust v12 at half kelly.

    Per hub note #299 + 3-day replay: when v9 + v12 disagree direction, v12
    is right 51.5% on AVERAGE-bigger-stake trades. Net +$398/3d swing
    arises from this asymmetry. Half-stake (0.5x kelly) bounds blast
    radius if the contrarian edge fades.
    """
    contrarian_enabled = _v12_contrarian_enabled()
    contrarian_min_dist = _v12_contrarian_min_dist()
    kelly_mod = _v12_contrarian_kelly_modifier()

    base_meta = {
        "probability_lgb": p_v9,
        "probability_lgb_v12": p_v12,
        "dir_v9": dir_v9,
        "dir_v12": dir_v12,
        "dist_v9": round(dist_v9, 4),
        "dist_v12": round(dist_v12, 4),
        "direction_agree": False,
    }

    if not contrarian_enabled:
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
            skip_reason="v9_v12_direction_disagreement_contrarian_disabled",
            metadata={**base_meta, "v12_contrarian_mode": False},
        )

    if dist_v12 < contrarian_min_dist:
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
            skip_reason="disagreement_v12_weak",
            metadata={
                **base_meta,
                "v12_contrarian_min_dist": contrarian_min_dist,
                "v12_contrarian_mode": False,
            },
        )

    # ── Trade v12's direction at 0.5x kelly ──────────────────────────────
    # Swap probability_lgb=p_v12 so the v9_ensemble stack derives v12's
    # direction. After delegation we halve collateral_pct.
    decision = _delegate_with_swapped_lgb(surface, p_v12)

    new_coll = (
        decision.collateral_pct * kelly_mod
        if decision.collateral_pct is not None
        else None
    )

    meta = dict(decision.metadata or {})
    meta["probability_classifier"] = None
    meta["probability_lgb"] = p_v9
    meta["probability_lgb_v12"] = p_v12
    meta["dist_v9"] = round(dist_v9, 4)
    meta["dist_v12"] = round(dist_v12, 4)
    meta["dir_v9"] = dir_v9
    meta["dir_v12"] = dir_v12
    meta["direction_agree"] = False
    meta["lgb_only_forced"] = True
    meta["v12_combo_model"] = True
    meta["v12_contrarian_mode"] = True
    meta["kelly_half_modifier"] = kelly_mod
    meta["collateral_pct_pre_halving"] = decision.collateral_pct

    return StrategyDecision(
        action=decision.action,
        direction=decision.direction,  # follows p_v12 → v12's direction
        confidence=decision.confidence,
        confidence_score=min(dist_v12 * 2, 1.0),
        entry_cap=decision.entry_cap,
        collateral_pct=new_coll,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            decision.entry_reason.replace("v9_ensemble", "v12_lgb_combo").replace(
                "v8_champion_lgb_only", "v12_lgb_combo"
            )
            if decision.entry_reason
            else ""
        ),
        skip_reason=decision.skip_reason,
        metadata=meta,
    )
