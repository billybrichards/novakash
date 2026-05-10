"""Hook for v_consensus_4way — 4-model probabilistic consensus gate for BTC 5m.

Strategy fires ONLY when all 4 probability sources agree on direction
with conviction (>= 0.55 for UP, <= 0.45 for DOWN). No delegation to
v9_ensemble, no surface swapping. Self-contained single check.

The 4 signals (all from /v4/snapshot surface, accessed via FullDataSurface):
  1. v2_probability_up           — V2/TimesFM scorer effective P(UP)
  2. probability_lgb_v9_1        — v9.1 priceToBeat-aligned LGB
  3. probability_lgb_v12         — v12 LGB
  4. probability_classifier      — Path1 MLP classifier head

May 1-8 data sweep (market_data resolved outcomes):
  >=0.55 / <=0.45 → 549 windows, 88.3% accuracy (~69/day)
  >=0.60 / <=0.40 → 305 windows, 88.9% accuracy (~38/day)
  >=0.65 / <=0.35 → 112 windows, 94.6% accuracy (~14/day)

Default: 0.55/0.45 for max volume. Tuneable via runtime override.

Graceful degradation:
  - Any signal is None → SKIP (model not loaded)
  - Consensus fails → SKIP (models disagree)

No fill-band gate — consensus windows naturally cluster at clob_ask >= 0.78
because the market rarely agrees with 4 models simultaneously. The contrarian
fill premium provides the edge.

Post-hook declarative gates (from YAML):
  - chainlink_freshness
  - cell_pause
  - Standard cooldown/dedup via gate_params
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v_consensus_4way"
_VERSION = "1.0.0"

# ── Consensus thresholds ─────────────────────────────────────────────────
_CONSENSUS_UP_MIN = 0.55   # All 4 signals must be >= this for UP
_CONSENSUS_DN_MAX = 0.45   # All 4 signals must be <= this for DOWN


def _confidence_label(score: float) -> str:
    """Map mean probability distance from 0.5 to DECISIVE/HIGH/MODERATE/LOW."""
    dist = abs(score - 0.5)
    if dist >= 0.30:
        return "DECISIVE"
    if dist >= 0.20:
        return "HIGH"
    if dist >= 0.10:
        return "MODERATE"
    return "LOW"


def evaluate_consensus_4way(surface: "FullDataSurface") -> StrategyDecision:
    """Check 4-model consensus and return TRADE or SKIP.

    Reads v2_probability_up, probability_lgb_v9_1, probability_lgb_v12,
    and probability_classifier from the surface. SKIPs gracefully if any
    are None (model not loaded on the scoring service).
    """
    # ── Read all 4 signals from surface ──────────────────────────────────
    p_v2 = surface.v2_probability_up
    p_v91 = getattr(surface, "probability_lgb_v9_1", None)
    p_v12 = surface.probability_lgb_v12
    p_cls = surface.probability_classifier

    signals = {
        "v2_probability_up": p_v2,
        "probability_lgb_v9_1": p_v91,
        "probability_lgb_v12": p_v12,
        "probability_classifier": p_cls,
    }

    # ── Graceful degradation if any signal is missing ────────────────────
    missing = [k for k, v in signals.items() if v is None]
    if missing:
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
            skip_reason="models_not_loaded",
            metadata={
                "missing_signals": missing,
                "consensus_4way_active": True,
            },
        )

    # ── Check consensus ──────────────────────────────────────────────────
    all_up = all(v >= _CONSENSUS_UP_MIN for v in signals.values())
    all_dn = all(v <= _CONSENSUS_DN_MAX for v in signals.values())

    if not all_up and not all_dn:
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
            skip_reason="consensus_disagreement",
            metadata={
                "signals": {k: round(float(v), 4) for k, v in signals.items()},
                "consensus_4way_active": True,
            },
        )

    # ── Consensus achieved — fire ────────────────────────────────────────
    mean_prob = sum(signals.values()) / 4.0  # type: ignore[arg-type]
    direction = "UP" if all_up else "DOWN"
    dist = abs(mean_prob - 0.5)
    confidence_score = min(dist * 2.0, 1.0)  # Scale: 0.55→0.10, 0.70→0.40, 0.80→0.60

    # entry_cap is the GTC price ceiling fed into fak_ladder_executor.
    # 0.0 would cause gtc_price = round(0.0 + pi_bonus, 2) ≈ $0.01 →
    # order rests at $0.01 and never fills. Read fill_band_max from runtime
    # overrides so operator can tune via SQL without a redeploy.
    fill_cap = _gp.get_float("fill_band_max", None, 0.82)

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=_confidence_label(mean_prob),
        confidence_score=confidence_score,
        entry_cap=fill_cap,  # GTC price ceiling — must be > 0
        collateral_pct=0.0,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=f"consensus_4way_{direction}_{mean_prob:.3f}",
        skip_reason="",
        metadata={
            "signals": {k: round(float(v), 4) for k, v in signals.items()},
            "mean_probability": round(float(mean_prob), 4),
            "consensus_4way_active": True,
        },
    )
