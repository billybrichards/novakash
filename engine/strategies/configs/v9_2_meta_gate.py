"""V9.2 meta gate strategy hook. V9.2 must have conviction AND gate must agree."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

_STRATEGY_ID = "v9_2_meta_gate"
_VERSION = "1.0.0"
_DEFAULT_GATE_THRESHOLD = 0.6
_DEFAULT_V92_UP = 0.75
_DEFAULT_V92_DOWN = 0.22


def evaluate_v9_2_meta_gate(surface: "FullDataSurface") -> StrategyDecision:
    p_v92 = getattr(surface, "probability_lgb_v9_2", None)
    p_gate = getattr(surface, "probability_v9_2_meta_gate", None)

    if p_v92 is None:
        return StrategyDecision(
            action="SKIP", direction=None, confidence=None,
            confidence_score=0.0, entry_cap=0.0, collateral_pct=0.0,
            strategy_id=_STRATEGY_ID, strategy_version=_VERSION,
            entry_reason="", skip_reason="v9_2_not_loaded",
            metadata={"probability_lgb_v9_2": None},
        )

    gate_threshold = _gp.get_float("meta_gate_threshold", None, _DEFAULT_GATE_THRESHOLD)
    v92_up = _gp.get_float("v9_2_up_threshold", None, _DEFAULT_V92_UP)
    v92_down = _gp.get_float("v9_2_down_threshold", None, _DEFAULT_V92_DOWN)

    has_conviction = p_v92 >= v92_up or p_v92 <= v92_down
    if not has_conviction:
        return StrategyDecision(
            action="SKIP", direction=None, confidence=None,
            confidence_score=0.0, entry_cap=0.0, collateral_pct=0.0,
            strategy_id=_STRATEGY_ID, strategy_version=_VERSION,
            entry_reason="", skip_reason="v9_2_below_conviction",
            metadata={"probability_lgb_v9_2": p_v92, "probability_v9_2_meta_gate": p_gate},
        )

    if p_gate is not None and p_gate < gate_threshold:
        return StrategyDecision(
            action="SKIP", direction=None, confidence=None,
            confidence_score=0.0, entry_cap=0.0, collateral_pct=0.0,
            strategy_id=_STRATEGY_ID, strategy_version=_VERSION,
            entry_reason="", skip_reason="v9_2_meta_gate_rejected",
            metadata={"probability_lgb_v9_2": p_v92, "probability_v9_2_meta_gate": p_gate},
        )

    direction = "UP" if p_v92 >= 0.5 else "DOWN"
    confidence = "HIGH" if abs(p_v92 - 0.5) >= 0.20 else "MODERATE"
    confidence_score = float(abs(p_v92 - 0.5) * 2.0)

    return StrategyDecision(
        action="TRADE", direction=direction, confidence=confidence,
        confidence_score=confidence_score, entry_cap=None, collateral_pct=None,
        strategy_id=_STRATEGY_ID, strategy_version=_VERSION,
        entry_reason="v9_2_meta_gate_pass",
        metadata={"probability_lgb_v9_2": p_v92, "probability_v9_2_meta_gate": p_gate},
    )
