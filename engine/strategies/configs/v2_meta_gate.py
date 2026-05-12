"""V2 meta gate strategy hook. V2 must have conviction AND gate must agree."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v2_meta_gate"
_VERSION = "1.0.0"
_DEFAULT_GATE_THRESHOLD = 0.6
_DEFAULT_V2_UP = 0.60
_DEFAULT_V2_DOWN = 0.40


def evaluate_v2_meta_gate(surface: "FullDataSurface") -> StrategyDecision:
    p_v2 = getattr(surface, "v2_probability_up", None)
    p_gate = getattr(surface, "probability_v2_meta_gate", None)

    if p_v2 is None:
        return StrategyDecision(
            action="SKIP", direction=None, confidence=None,
            confidence_score=0.0, entry_cap=0.0, collateral_pct=0.0,
            strategy_id=_STRATEGY_ID, strategy_version=_VERSION,
            entry_reason="", skip_reason="v2_not_loaded",
            metadata={"v2_probability_up": None},
        )

    gate_threshold = _gp.get_float("meta_gate_threshold", None, _DEFAULT_GATE_THRESHOLD)
    v2_up = _gp.get_float("v2_up_threshold", None, _DEFAULT_V2_UP)
    v2_down = _gp.get_float("v2_down_threshold", None, _DEFAULT_V2_DOWN)

    has_conviction = p_v2 >= v2_up or p_v2 <= v2_down
    if not has_conviction:
        return StrategyDecision(
            action="SKIP", direction=None, confidence=None,
            confidence_score=0.0, entry_cap=0.0, collateral_pct=0.0,
            strategy_id=_STRATEGY_ID, strategy_version=_VERSION,
            entry_reason="", skip_reason="v2_below_conviction",
            metadata={"v2_probability_up": p_v2, "probability_v2_meta_gate": p_gate},
        )

    if p_gate is not None and p_gate < gate_threshold:
        return StrategyDecision(
            action="SKIP", direction=None, confidence=None,
            confidence_score=0.0, entry_cap=0.0, collateral_pct=0.0,
            strategy_id=_STRATEGY_ID, strategy_version=_VERSION,
            entry_reason="", skip_reason="v2_meta_gate_rejected",
            metadata={"v2_probability_up": p_v2, "probability_v2_meta_gate": p_gate},
        )

    direction = "UP" if p_v2 >= 0.5 else "DOWN"
    confidence = "HIGH" if abs(p_v2 - 0.5) >= 0.20 else "MODERATE"
    confidence_score = float(abs(p_v2 - 0.5) * 2.0)

    return StrategyDecision(
        action="TRADE", direction=direction, confidence=confidence,
        confidence_score=confidence_score, entry_cap=None, collateral_pct=None, skip_reason=None,
        strategy_id=_STRATEGY_ID, strategy_version=_VERSION,
        entry_reason="v2_meta_gate_pass",
        metadata={"v2_probability_up": p_v2, "probability_v2_meta_gate": p_gate},
    )
