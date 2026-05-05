"""Hook for v9_1_lgb_only_relaxed_dn — DOWN-floor relaxation soak variant.

Same Python evaluation as ``v9_1_lgb_only`` (delegates to
``evaluate_v9_1_lgb_only`` so the v9.1 booster + v9 ensemble gate stack are
identical). Two YAML differences + one CARVE-OUT enforced in this wrapper.

YAML differences (gate_params)
------------------------------
1. ``lgb_dist_min_down`` lowered from production runtime override (0.25)
   to 0.15 — opens the 0.15-0.25 DOWN dist sub-buckets.
2. ``lgb_dist_min_up`` kept at production strict floor (0.20). UP
   relaxation rejected per Wilson analysis (Hub note #342) — relaxed UP
   Wilson lower 54.8% < breakeven 68.2%.

CARVE-OUT enforced in this wrapper hook
---------------------------------------
The 0.20-0.25 DOWN dist sub-bucket is the WEAKEST in the entire DOWN
ladder (65.5% WR, n=29). User explicitly asked it be EXCLUDED from the
soak — we want clean evidence on the empirically-strong 0.15-0.20 zones,
not contaminated data from the bad mid-band.

The wrapper delegates to evaluate_v9_1_lgb_only first (so all gate
evaluation, oracle checks, regime gates, etc. run unchanged). Then if
the parent returned TRADE on DOWN with dist in the bad band [0.20, 0.25),
the wrapper overrides to SKIP with a dedicated reason for soak analysis.

Net effect — variant fires for:
  - DOWN: dist in [0.15, 0.20) — relaxed-good zones (currently blocked by prod)
  - DOWN: dist >= 0.25 — overlaps with production v9_1_lgb_only LIVE
                          (gives co-fire baseline for soak comparison)
  - UP:   dist >= 0.20 — matches production strict floor
  - UP:   dist <  0.20 — SKIP via parent floor

The 0.20-0.25 DOWN zone is excluded ENTIRELY (carve-out).

If you want to also exclude the >= 0.25 high-conviction overlap with
production (cleaner isolation but loses co-fire baseline), change the
carve-out below from ``0.20 <= d < 0.25`` to ``d >= 0.20``.

Hub note #342 — full conviction analysis + Wilson math + half-stake design.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs.v9_1_lgb_only import evaluate_v9_1_lgb_only as _evaluate_v9_1

_STRATEGY_ID = "v9_1_lgb_only_relaxed_dn"
_VERSION = "9.1.0-relaxed-dn"

# Carve-out band: DOWN dist sub-bucket [0.20, 0.25) is the weakest in the
# entire DOWN ladder (65.5% WR n=29) — explicitly excluded from soak.
# Half-open interval [lo, hi) so dist == 0.25 falls into the production-strict
# zone (NOT carved out — produces co-fire baseline with v9_1_lgb_only LIVE).
_CARVE_OUT_DN_DIST_LO = 0.20
_CARVE_OUT_DN_DIST_HI = 0.25
_CARVE_OUT_SKIP_REASON = "carve_out_dn_dist_band_0.20_0.25"


def _down_dist(p_v9_1: float | None) -> float | None:
    """Distance-below-coinflip when probability indicates DOWN, else None.

    Returns None if probability is None or >= 0.50 (probability indicates
    UP — carve-out doesn't apply).
    """
    if p_v9_1 is None:
        return None
    if p_v9_1 >= 0.5:
        return None
    return 0.5 - p_v9_1


def evaluate_v9_1_lgb_only_relaxed_dn(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Wrapper around evaluate_v9_1_lgb_only with DOWN-mid-band carve-out.

    Step 1: Delegate to evaluate_v9_1_lgb_only — runs full gate stack
            with the relaxed YAML floor (0.15) plumbed via gate_params.
    Step 2: If parent returned TRADE on DOWN with dist in the carve-out
            band, override to SKIP. Otherwise relabel identity and pass.
    """
    decision = _evaluate_v9_1(surface)

    # Carve-out enforcement: only applies to TRADE-DOWN decisions
    if decision.action == "TRADE" and decision.direction == "DOWN":
        p_v9_1 = getattr(surface, "probability_lgb_v9_1", None)
        dist = _down_dist(p_v9_1)
        if dist is not None and _CARVE_OUT_DN_DIST_LO <= dist < _CARVE_OUT_DN_DIST_HI:
            # Bad sub-bucket — override TRADE to SKIP
            meta = dict(decision.metadata or {})
            meta["carve_out_applied"] = True
            meta["carve_out_band"] = (
                f"[{_CARVE_OUT_DN_DIST_LO}, {_CARVE_OUT_DN_DIST_HI})"
            )
            meta["carve_out_dist"] = dist
            meta["parent_action"] = "TRADE"
            meta["parent_entry_reason"] = decision.entry_reason
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
                skip_reason=_CARVE_OUT_SKIP_REASON,
                metadata=meta,
            )

    # No carve-out triggered — relabel identity to variant and pass through.
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
            decision.entry_reason.replace("v9_1_lgb_only", _STRATEGY_ID)
            if decision.entry_reason
            else ""
        ),
        skip_reason=decision.skip_reason,
        metadata=dict(decision.metadata or {}),
    )
