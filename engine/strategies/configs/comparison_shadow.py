"""Comparison shadow strategy — score every window using a configurable signal source.

Used to A/B compare v9 / v10 / v12 / blend / classifier predictions across
shadow strategy variants. Mode is always GHOST — never places live trades.

YAML variants like ``v4_fusion_compare_v9.yaml`` override
``gate_params.signal_source`` to one of:

  probability_lgb           (= v9 raw, prod v5 LGB)
  probability_lgb_v10       (= v10 raw)
  probability_lgb_v12       (= v12 raw, None until v12 ships server-side)
  probability_up            (= 50/50 blend lgb + classifier, current poly_confidence)
  probability_classifier    (= TimesFM classifier head)
  combo_v9_v12_avg          (= avg(probability_lgb, probability_lgb_v12) when both present)

Decisions are emitted with ``metadata.signal_source`` + ``metadata.probability_used``
so ``strategy_decisions`` can be joined on ``window_ts`` to compare WR across
variants. See hub note #285 for the comparison framework, PR #431 for shipping
context.

Hard guarantees (called out for reviewers):
  * Hook NEVER returns a live-execution decision — variant YAMLs all set
    ``mode: GHOST`` AND ``sizing.fraction: 0.0`` so even if mode were
    accidentally flipped, sizing is still zero.
  * No mutation of the surface (read-only access).
  * No side effects (no DB writes, no logging beyond what registry does).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_VERSION = "1.0.0-shadow"


def _read_signal(surface: "FullDataSurface", source: str) -> Optional[float]:
    """Look up the signal from the named field on the surface.

    Returns ``None`` when the requested source is not present (caller turns
    that into a SKIP with ``signal_source_unavailable:<source>``).
    """
    if source == "probability_lgb":
        return getattr(surface, "probability_lgb", None)
    if source == "probability_lgb_v10":
        return getattr(surface, "probability_lgb_v10", None)
    if source == "probability_lgb_v12":
        return getattr(surface, "probability_lgb_v12", None)
    if source == "probability_up":
        # Blend = (lgb + classifier) / 2 if classifier present, else lgb.
        # Mirrors the historical poly_confidence blend semantics.
        lgb = getattr(surface, "probability_lgb", None)
        clf = getattr(surface, "probability_classifier", None)
        if lgb is None:
            return None
        if clf is None:
            return lgb
        return (lgb + clf) / 2
    if source == "probability_classifier":
        return getattr(surface, "probability_classifier", None)
    if source == "combo_v9_v12_avg":
        v9 = getattr(surface, "probability_lgb", None)
        v12 = getattr(surface, "probability_lgb_v12", None)
        if v9 is None or v12 is None:
            return None
        return (v9 + v12) / 2
    # Unknown source — caller treats as unavailable.
    return None


def evaluate_comparison_shadow(surface: "FullDataSurface") -> StrategyDecision:
    """Score the window using ``gate_params.signal_source``.

    Always emits a decision (TRADE or SKIP). All variants run in GHOST
    mode per their YAML — the engine never places an order from these.
    """
    source = _gp.get_str("signal_source", None, "probability_lgb")
    strategy_id = _gp.get_str(
        "shadow_strategy_id", None, f"shadow_{source}"
    )
    floor = _gp.get_float("shadow_conviction_floor", None, 0.10)

    p = _read_signal(surface, source)

    if p is None:
        return StrategyDecision(
            action="SKIP",
            direction=None,
            confidence=None,
            confidence_score=0.0,
            entry_cap=None,
            collateral_pct=None,
            strategy_id=strategy_id,
            strategy_version=_VERSION,
            entry_reason="",
            skip_reason=f"signal_source_unavailable:{source}",
            metadata={
                "signal_source": source,
                "probability_used": None,
                "shadow_mode": True,
            },
        )

    direction = "UP" if p > 0.5 else "DOWN"
    distance = abs(p - 0.5)

    # Apply the same conviction floor as the v9_lgb_only baseline (0.10) so
    # comparisons are fair across variants. Override per-variant via YAML.
    if distance < floor:
        return StrategyDecision(
            action="SKIP",
            direction=None,
            confidence=None,
            confidence_score=distance * 2,
            entry_cap=None,
            collateral_pct=None,
            strategy_id=strategy_id,
            strategy_version=_VERSION,
            entry_reason="",
            skip_reason=(
                f"shadow_below_floor dist={distance:.3f} signal={source}"
            ),
            metadata={
                "signal_source": source,
                "probability_used": p,
                "distance": distance,
                "shadow_mode": True,
            },
        )

    # Pass: emit a "would TRADE" decision. mode=GHOST in YAML prevents the
    # engine from actually placing an order; sizing.fraction=0.0 belt-and-
    # braces. Decision lands in strategy_decisions for SQL-level WR analysis.
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=None,
        confidence_score=distance * 2,
        entry_cap=None,
        collateral_pct=None,
        strategy_id=strategy_id,
        strategy_version=_VERSION,
        entry_reason=f"shadow_signal:{source} p={p:.3f} dir={direction}",
        skip_reason=None,
        metadata={
            "signal_source": source,
            "probability_used": p,
            "distance": distance,
            "shadow_mode": True,
        },
    )
