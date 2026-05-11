"""Hook for v13_edge — early-window UP-only strategy.

Fires when 180-280 seconds remain before window close (early window,
T-minus convention). UP only — DOWN is proven negative EV at this timing.

Three signal tiers (first match wins, all UP only):
  T1: v2_probability_up >= 0.60 AND probability_lgb_v9_2 >= 0.55  -> 0.08 collateral
  T2: v2_probability_up >= 0.60 AND probability_lgb_v12 >= 0.55   -> 0.06 collateral
  T3: probability_lgb_v9_2 >= 0.70 solo                            -> 0.04 collateral

GTC resting price is set to 0.72 — well below the 0.89 FAK cap — so it
only fills if someone dumps liquidity cheap. This is achieved via
gtc_cap=0.72 on the StrategyDecision, which the FAK ladder executor reads
and uses instead of its default (entry_cap + pi_bonus).

Hub note #464: V2+V92/V12 at T-180-280s = 94-96% WR, +$0.11 EV.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision

_STRATEGY_ID = "v13_edge"
_VERSION = "13.0.0"

# Entry cap — max acceptable CLOB ask price for the UP token.
_ENTRY_CAP: float = 0.89

# GTC resting price — patient limit order after FAK misses.
# Set well below the FAK cap so it only fills on liquidity dumps.
_GTC_CAP: float = 0.72


def evaluate_v13_edge(surface: "FullDataSurface") -> StrategyDecision:
    """Evaluate v13_edge early-window UP-only strategy.

    Independent evaluation — does NOT delegate to v9_ensemble or any other
    strategy's evaluate function.

    Returns a TRADE decision if a tier matches (UP only), SKIP otherwise.
    """
    v2: Optional[float] = getattr(surface, "v2_probability_up", None)
    v92: Optional[float] = getattr(surface, "probability_lgb_v9_2", None)
    v12: Optional[float] = getattr(surface, "probability_lgb_v12", None)
    clob_ask: Optional[float] = getattr(surface, "clob_up_ask", None)

    # Gate: fill price check — skip if the current CLOB ask is already
    # above our entry cap (we won't get a sensible fill).
    if clob_ask is not None and clob_ask > _ENTRY_CAP:
        return StrategyDecision(
            action="SKIP",
            direction=None,
            confidence=None,
            confidence_score=0.0,
            entry_cap=_ENTRY_CAP,
            collateral_pct=None,
            strategy_id=_STRATEGY_ID,
            strategy_version=_VERSION,
            entry_reason="",
            skip_reason=f"fill_too_high: {clob_ask:.3f} > {_ENTRY_CAP}",
            metadata={
                "v2_probability_up": v2,
                "probability_lgb_v9_2": v92,
                "probability_lgb_v12": v12,
                "clob_up_ask": clob_ask,
            },
        )

    # Tier evaluation — first match wins, all UP only.
    tier: Optional[str] = None
    collateral_pct: Optional[float] = None

    if v2 is not None and v92 is not None and v2 >= 0.60 and v92 >= 0.55:
        tier, collateral_pct = "T1", 0.08
    elif v2 is not None and v12 is not None and v2 >= 0.60 and v12 >= 0.55:
        tier, collateral_pct = "T2", 0.06
    elif v92 is not None and v92 >= 0.70:
        tier, collateral_pct = "T3", 0.04

    if tier is None:
        return StrategyDecision(
            action="SKIP",
            direction=None,
            confidence=None,
            confidence_score=0.0,
            entry_cap=_ENTRY_CAP,
            collateral_pct=None,
            strategy_id=_STRATEGY_ID,
            strategy_version=_VERSION,
            entry_reason="",
            skip_reason=(
                f"no_tier_match: v2={v2} v92={v92} v12={v12}"
            ),
            metadata={
                "v2_probability_up": v2,
                "probability_lgb_v9_2": v92,
                "probability_lgb_v12": v12,
                "clob_up_ask": clob_ask,
            },
        )

    # confidence_score maps to 0.4-0.8 range (collateral_pct * 10).
    confidence_score = collateral_pct * 10  # 0.04 -> 0.4, 0.06 -> 0.6, 0.08 -> 0.8

    return StrategyDecision(
        action="TRADE",
        direction="UP",
        confidence=tier,
        confidence_score=confidence_score,
        entry_cap=_ENTRY_CAP,
        collateral_pct=collateral_pct,
        gtc_cap=_GTC_CAP,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=f"{tier}: v2={v2} v92={v92} v12={v12}",
        skip_reason=None,
        metadata={
            "tier": tier,
            "v2_probability_up": v2,
            "probability_lgb_v9_2": v92,
            "probability_lgb_v12": v12,
            "clob_up_ask": clob_ask,
        },
    )
