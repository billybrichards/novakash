"""Custom hooks for v4_fusion strategy.

Ports the polymarket_v2 evaluation path from V4FusionStrategy.
This handles the complex timing/CLOB-divergence logic that doesn't
reduce to simple gate configs.

Three evaluation paths:
1. polymarket_v2: clean venue-specific recommendation (preferred)
2. polymarket legacy: old timesfm builds with venue="polymarket" in extras
3. legacy margin-engine: regime + consensus + conviction gates
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision

_STRATEGY_ID = "v15m_fusion"
_VERSION = "1.0.0"

# Conviction -> minimum distance from 0.5 (legacy path only)
_CONVICTION_THRESHOLDS = {
    "HIGH": 0.12,
    "MEDIUM": 0.15,
    "LOW": 0.20,
    "NONE": 1.0,
}

_TRADEABLE_REGIMES = {"calm_trend", "volatile_trend"}


def _gate(name: str, passed: bool, reason: str) -> dict:
    return {"gate": name, "passed": passed, "reason": reason}


def _skip(reason: str, gate_results: Optional[list[dict]] = None) -> StrategyDecision:
    return StrategyDecision(
        action="SKIP",
        direction=None,
        confidence=None,
        confidence_score=None,
        entry_cap=None,
        collateral_pct=None,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason="",
        skip_reason=reason,
        metadata={"gate_results": gate_results or []},
    )


def evaluate_polymarket_v2(surface: "FullDataSurface") -> Optional[StrategyDecision]:
    """Full V4 fusion evaluation ported from V4FusionStrategy.

    Returns StrategyDecision if handled, None to fall through to gates.
    """
    # Check if we have polymarket outcome data
    if surface.poly_direction is not None:
        return _evaluate_poly_v2(surface)

    # Check for legacy polymarket path
    if surface.v4_recommended_side in ("UP", "DOWN"):
        return _evaluate_poly_legacy(surface)

    # Legacy margin-engine path
    return _evaluate_legacy(surface)


def _evaluate_poly_v2(surface: "FullDataSurface") -> StrategyDecision:
    """Polymarket v2 evaluation -- clean venue-specific recommendation.

    Timing gates:
      - early (>T-250): hard skip
      - optimal (T-180 to T-250): trade if confidence passes
      - late (<T-180): hard skip
    """
    direction = surface.poly_direction
    trade_advised = surface.poly_trade_advised or False
    confidence = surface.poly_confidence or 0.5
    distance = surface.poly_confidence_distance or abs(confidence - 0.5)
    reason = surface.poly_reason or "unknown"
    max_entry = surface.poly_max_entry_price
    gates: list[dict] = []

    # Replace server timing label with offset-based derivation (server may be 5m-calibrated)
    offset = surface.eval_offset or 0
    if offset > 250:
        timing = "early"
    elif offset >= 180:
        timing = "optimal"
    else:
        timing = "late"

    # Too early / too late
    if timing in ("early", "late"):
        gates.append(_gate("timing", False, f"timing={timing} outside trade window"))
        return _skip(f"polymarket: timing={timing} -- outside window", gates)

    gates.append(_gate("timing", True, f"timing={timing} in [180,250]"))

    # Confidence gate
    if distance < 0.12:
        gates.append(_gate("confidence", False, f"dist={distance:.3f} < 0.12"))
        return _skip(
            f"polymarket: p_up={confidence:.3f} dist={distance:.3f} < 0.12 threshold",
            gates,
        )

    gates.append(_gate("confidence", True, f"dist={distance:.3f} >= 0.12"))

    # trade_advised disabled for 15m — TimesFM assembler's regime/fee gates are
    # calibrated for 5m and incorrectly block high-conviction 15m signals.
    # Engine-side gates (timing, confidence, direction) are sufficient.
    # if not trade_advised:
    #     return _skip(f"polymarket: {reason} (timing={timing}, dist={distance:.3f})")

    if not direction:
        gates.append(_gate("direction", False, "no direction"))
        return _skip("polymarket: no direction", gates)

    gates.append(_gate("direction", True, f"direction={direction}"))

    # Macro direction gate
    macro_gate = surface.v4_macro_direction_gate
    if macro_gate and macro_gate not in ("ALLOW_ALL", None):
        if macro_gate == "LONG_ONLY" and direction == "DOWN":
            gates.append(_gate("macro_direction", False, "LONG_ONLY blocks DOWN"))
            return _skip("macro direction_gate=LONG_ONLY blocks DOWN", gates)
        if macro_gate == "SHORT_ONLY" and direction == "UP":
            gates.append(_gate("macro_direction", False, "SHORT_ONLY blocks UP"))
            return _skip("macro direction_gate=SHORT_ONLY blocks UP", gates)

    if macro_gate:
        gates.append(_gate("macro_direction", True, f"{macro_gate} allows {direction}"))

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=surface.v4_conviction or f"dist_{distance:.2f}",
        confidence_score=distance * 2.0,
        entry_cap=max_entry,
        collateral_pct=surface.v4_recommended_collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=f"polymarket_{reason}_T{surface.eval_offset}",
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "poly_direction": direction,
            "poly_confidence_distance": distance,
            "poly_timing": timing,
            "v4_regime": surface.v4_regime,
        },
    )


def _evaluate_poly_legacy(surface: "FullDataSurface") -> StrategyDecision:
    """Legacy polymarket path for old timesfm builds."""
    p_up = surface.v2_probability_up or 0.5
    distance = abs(p_up - 0.5)
    direction = surface.v4_recommended_side or ("UP" if p_up > 0.5 else "DOWN")
    gates: list[dict] = []

    if distance < 0.12:
        gates.append(_gate("confidence", False, f"dist={distance:.3f} < 0.12"))
        return _skip(f"polymarket_legacy: dist={distance:.3f} < 0.12", gates)

    gates.append(_gate("confidence", True, f"dist={distance:.3f} >= 0.12"))

    # Macro gate
    macro_gate = surface.v4_macro_direction_gate
    if macro_gate and macro_gate not in ("ALLOW_ALL", None):
        if macro_gate == "LONG_ONLY" and direction == "DOWN":
            gates.append(_gate("macro_direction", False, "LONG_ONLY blocks DOWN"))
            return _skip("macro direction_gate=LONG_ONLY blocks DOWN", gates)
        if macro_gate == "SHORT_ONLY" and direction == "UP":
            gates.append(_gate("macro_direction", False, "SHORT_ONLY blocks UP"))
            return _skip("macro direction_gate=SHORT_ONLY blocks UP", gates)

    if macro_gate:
        gates.append(_gate("macro_direction", True, f"{macro_gate} allows {direction}"))

    collateral_pct = surface.v4_recommended_collateral_pct
    if collateral_pct is not None and surface.v4_macro_size_modifier:
        collateral_pct *= surface.v4_macro_size_modifier

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=surface.v4_conviction,
        confidence_score=surface.v4_conviction_score or (distance * 2.0),
        entry_cap=surface.poly_max_entry_price,
        collateral_pct=collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=f"polymarket_legacy_dist{distance:.2f}_T{surface.eval_offset}",
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "v2_probability_up": p_up,
            "v4_regime": surface.v4_regime,
        },
    )


def _evaluate_legacy(surface: "FullDataSurface") -> StrategyDecision:
    """Legacy margin-engine path (non-polymarket templates)."""
    # Gate 1: Regime
    regime = surface.v4_regime
    gates: list[dict] = []
    if regime and regime not in _TRADEABLE_REGIMES:
        gates.append(_gate("regime", False, f"regime={regime} not tradeable"))
        return _skip(f"regime={regime} not tradeable", gates)

    gates.append(_gate("regime", True, f"regime={regime} tradeable"))

    # Gate 2: Consensus
    if not surface.v4_consensus_safe_to_trade:
        gates.append(_gate("consensus", False, "consensus not safe_to_trade"))
        return _skip("consensus not safe_to_trade", gates)

    gates.append(_gate("consensus", True, "safe_to_trade=true"))

    # Gate 3: Conviction threshold
    p_up = surface.v2_probability_up
    if p_up is None:
        gates.append(_gate("confidence", False, "no probability_up"))
        return _skip("no probability_up", gates)
    distance = abs(p_up - 0.5)
    conviction = surface.v4_conviction or "NONE"
    min_dist = _CONVICTION_THRESHOLDS.get(conviction, 1.0)
    if distance < min_dist:
        gates.append(
            _gate("confidence", False, f"dist={distance:.2f} < {min_dist:.2f}")
        )
        return _skip(
            f"conviction={conviction} requires dist={min_dist:.2f}, got {distance:.2f}",
            gates,
        )

    gates.append(_gate("confidence", True, f"dist={distance:.2f} >= {min_dist:.2f}"))

    # Gate 4: Direction
    direction = surface.v4_recommended_side
    if direction is None:
        direction = "UP" if p_up > 0.5 else "DOWN"

    # Gate 5: Macro
    macro_gate = surface.v4_macro_direction_gate
    if macro_gate and macro_gate not in ("ALLOW_ALL", None):
        if macro_gate == "LONG_ONLY" and direction == "DOWN":
            gates.append(_gate("macro_direction", False, "LONG_ONLY blocks DOWN"))
            return _skip("macro direction_gate=LONG_ONLY blocks DOWN", gates)
        if macro_gate == "SHORT_ONLY" and direction == "UP":
            gates.append(_gate("macro_direction", False, "SHORT_ONLY blocks UP"))
            return _skip("macro direction_gate=SHORT_ONLY blocks UP", gates)

    if macro_gate:
        gates.append(_gate("macro_direction", True, f"{macro_gate} allows {direction}"))

    collateral_pct = surface.v4_recommended_collateral_pct
    if collateral_pct is not None and surface.v4_macro_size_modifier:
        collateral_pct *= surface.v4_macro_size_modifier

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=conviction,
        confidence_score=surface.v4_conviction_score,
        entry_cap=None,
        collateral_pct=collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(f"v4_{conviction}_{regime}_T{surface.eval_offset}_p{p_up:.2f}"),
        skip_reason=None,
        metadata={
            "gate_results": gates,
            "v2_probability_up": p_up,
            "v4_regime": regime,
            "v4_conviction": conviction,
        },
    )
