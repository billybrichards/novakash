"""Hook for v12_lgb_combo — v9.1+v12 LGB combo strategy with disagreement-contrarian path.

2026-05-02 retrain swap: now reads `probability_lgb_v9_1` (v9.1 retrain,
priceToBeat-aligned) instead of `probability_lgb` (v9 PROD). Honest forward-
pass on 7d truly out-of-sample holdout shows +5.92pp weighted lift over
v9 PROD with ECE improved at 4/5 deltas. See hub note #313 +
docs/v9_1_PROVENANCE.md. Strategy SKIPs gracefully when v9.1 is None
(timesfm V9_1_ENABLED=false / model load fail) rather than falling back to
v9 PROD — clean swap, not a soft cutover.

Implements two trading modes:

* **Option A (agreement path)**: when v9.1 LGB (`probability_lgb_v9_1`) and
  v12 LGB (`probability_lgb_v12`) agree on direction AND
  `min(dist_v9_1, dist_v12) >= combo_min_dist`, trade the averaged
  probability `(p_v9_1 + p_v12) / 2`. Sizing via existing CLOBSizingGate
  (max 2.5x kelly — same as v10 baseline).

* **Option C (disagreement-contrarian path)**: when v9.1 + v12 disagree
  direction AND `dist_v12 >= v12_contrarian_min_dist`, trade in **v12's
  direction** at **0.5x kelly** (collateral_pct halved). Hub note #299 +
  3-day replay shows v12 is right 51.5% of the time on disagreements with a
  +$398/3d edge from asymmetry (v12-fix avg|pnl|=$12 vs v12-break $6.7).
  Half-stake bounds blast radius if the contrarian edge fades. Note:
  asymmetry signal was measured against v9 PROD, not v9.1 — needs 7d
  shadow re-validation before trusting the disagreement-contrarian edge
  in the v9.1 era.

In both paths the hook swaps probabilities onto the surface before delegating
to the v9_ensemble gate stack (same delegation pattern as v10_lgb_only.py),
and restores the original surface afterwards. Surface field swapped is still
`probability_lgb` (the gate stack consumes that name) — we just SOURCE the
combo probability from v9.1 rather than v9 PROD.

v12 model: retrained on window_snapshots with verified Polymarket labels.
3-day replay (vs v9 PROD): +$398 PnL improvement over v9 alone (228
trades). v12 catches v9's big losses while keeping 80% of v9's wins.

Runs as GHOST until human flips to LIVE. Promotion gate per
feedback_no_auto_model_promotion: 7d shadow with WR > prior v9+v12
baseline (~73% on 30d real trades, +$104 cum PnL on CASCADE×DOWN slice)
before any LIVE consideration.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp
from strategies.configs.v9_ensemble import evaluate_v9_ensemble as _evaluate_v9
from strategies.gates.cell_param_overrides import get_cell_param_overrides as _get_cell_param_overrides

_STRATEGY_ID = "v12_lgb_combo"
_VERSION = "12.0.0-combo-v9_1"

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
    """Evaluate v12_lgb_combo (Options A + C) with v9.1 retrain as the
    "v9-side" of the agreement.

    Decision logic:
    1. Read p_v9_1 (probability_lgb_v9_1, retrained) and p_v12
       (probability_lgb_v12).
    2. If either is None -> SKIP. v9.1 None means timesfm V9_1_ENABLED=false
       or v9.1 model failed to load -- clean SKIP rather than fall-back to
       v9 PROD, since the whole point of this swap is to validate the
       retrain forward-pass on shadow before LIVE.
    3. **Agreement path (A)**: if both directions match AND
       min(dist_v9_1, dist_v12) >= combo_min_dist, average probabilities
       and delegate to v9_ensemble at full kelly.
    4. **Disagreement path (C)**: if directions differ AND
       dist_v12 >= v12_contrarian_min_dist AND v12_contrarian_enabled,
       trust v12's call, delegate to v9_ensemble using p_v12, then
       halve the returned collateral_pct (0.5x kelly).
    5. Otherwise SKIP with an explicit reason.
    """
    # -- Read both probabilities ------------------------------------------
    # v9.1 retrain (priceToBeat-aligned) replaces v9 PROD as the "v9-side"
    # of the combo. Hub note #313 -- +5.92pp weighted lift on 7d holdout.
    p_v9_1 = getattr(surface, "probability_lgb_v9_1", None)
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
            metadata={
                "probability_lgb_v12": None,
                "probability_lgb_v9_1": p_v9_1,
            },
        )

    if p_v9_1 is None:
        # Clean SKIP -- do NOT fall back to v9 PROD. The point of this
        # combo variant is to validate v9.1 forward-pass; falling back
        # would silently mix v9.1 / v9 signal across the shadow window.
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
            skip_reason="probability_lgb_v9_1 unavailable",
            metadata={
                "probability_lgb_v12": p_v12,
                "probability_lgb_v9_1": None,
                "v9_1_enabled": False,
            },
        )

    # -- Direction split --------------------------------------------------
    dir_v9_1 = "UP" if p_v9_1 > 0.5 else "DOWN"
    dir_v12 = "UP" if p_v12 > 0.5 else "DOWN"
    dist_v9_1 = abs(p_v9_1 - 0.5)
    dist_v12 = abs(p_v12 - 0.5)

    if dir_v9_1 == dir_v12:
        return _evaluate_agreement_path(
            surface, p_v9_1, p_v12, dist_v9_1, dist_v12, dir_v9_1
        )

    # Directions disagree -- Option C contrarian path.
    return _evaluate_disagreement_path(
        surface, p_v9_1, p_v12, dist_v9_1, dist_v12, dir_v9_1, dir_v12
    )


def _evaluate_agreement_path(
    surface: "FullDataSurface",
    p_v9_1: float,
    p_v12: float,
    dist_v9_1: float,
    dist_v12: float,
    direction: str,
) -> StrategyDecision:
    """Option A -- both models agree direction. Trade averaged probability."""
    combo_dist = min(dist_v9_1, dist_v12)

    # -- Per-cell parameter override (hub #402 / PR #506) -----------------
    # Resolve combo_min_dist on a per-cell basis before applying the
    # strategy-level floor. Falls back to _combo_min_dist() when no cell
    # key matches (fail-open). NOT bypassable by VHC.
    _cell_overrides_v12 = _get_cell_param_overrides(
        params=_gp.get_dict("param_overrides_by_cell", default={}),
        direction=direction,
        eval_offset=getattr(surface, "eval_offset", None),
        regime=getattr(surface, "regime", None),
        window_ts=getattr(surface, "window_ts", None),
    )
    effective_combo_min = _cell_overrides_v12.get("combo_min_dist", _combo_min_dist())

    if combo_dist < effective_combo_min:
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
                "probability_lgb_v9_1": p_v9_1,
                "probability_lgb_v12": p_v12,
                "dist_v9_1": round(dist_v9_1, 4),
                "dist_v12": round(dist_v12, 4),
                "combo_dist": round(combo_dist, 4),
                "combo_min_dist": effective_combo_min,
                "direction_agree": True,
                "v12_contrarian_mode": False,
            },
        )

    p_combo = (p_v9_1 + p_v12) / 2.0
    decision = _delegate_with_swapped_lgb(surface, p_combo)

    meta = dict(decision.metadata or {})
    meta["probability_classifier"] = None
    meta["probability_lgb_v9_1"] = p_v9_1
    meta["probability_lgb_v12"] = p_v12
    meta["p_combo"] = round(p_combo, 6)
    meta["dist_v9_1"] = round(dist_v9_1, 4)
    meta["dist_v12"] = round(dist_v12, 4)
    meta["combo_dist"] = round(combo_dist, 4)
    meta["direction_agree"] = True
    meta["lgb_only_forced"] = True
    meta["v12_combo_model"] = True
    meta["v12_combo_v9_1_source"] = True
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
    p_v9_1: float,
    p_v12: float,
    dist_v9_1: float,
    dist_v12: float,
    dir_v9_1: str,
    dir_v12: str,
) -> StrategyDecision:
    """Option C -- v9.1 and v12 disagree direction. Trust v12 at half kelly.

    Per hub note #299 + 3-day replay: when v9 PROD + v12 disagreed direction,
    v12 was right 51.5% on AVERAGE-bigger-stake trades. Net +$398/3d swing
    arose from this asymmetry. Half-stake (0.5x kelly) bounds blast radius
    if the contrarian edge fades.

    Caveat (post-2026-05-02 v9.1 swap): the asymmetry signal was measured
    against v9 PROD, not v9.1. The retrain may shift the disagreement
    distribution; needs 7d shadow re-validation before trusting the
    contrarian edge in the v9.1 era. Set v12_contrarian_enabled=false in
    the YAML to canary-disable Option C while keeping A.
    """
    contrarian_enabled = _v12_contrarian_enabled()
    contrarian_min_dist = _v12_contrarian_min_dist()
    kelly_mod = _v12_contrarian_kelly_modifier()

    base_meta = {
        "probability_lgb_v9_1": p_v9_1,
        "probability_lgb_v12": p_v12,
        "dir_v9_1": dir_v9_1,
        "dir_v12": dir_v12,
        "dist_v9_1": round(dist_v9_1, 4),
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

    # -- Trade v12's direction at 0.5x kelly ----------------------------------
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
    meta["probability_lgb_v9_1"] = p_v9_1
    meta["probability_lgb_v12"] = p_v12
    meta["dist_v9_1"] = round(dist_v9_1, 4)
    meta["dist_v12"] = round(dist_v12, 4)
    meta["dir_v9_1"] = dir_v9_1
    meta["dir_v12"] = dir_v12
    meta["direction_agree"] = False
    meta["lgb_only_forced"] = True
    meta["v12_combo_model"] = True
    meta["v12_combo_v9_1_source"] = True
    meta["v12_contrarian_mode"] = True
    meta["kelly_half_modifier"] = kelly_mod
    meta["collateral_pct_pre_halving"] = decision.collateral_pct

    return StrategyDecision(
        action=decision.action,
        direction=decision.direction,  # follows p_v12 -> v12's direction
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
