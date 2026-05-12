"""V12 meta gate strategy hook. V12 must have conviction AND gate must agree."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision

from strategies import gate_params as _gp

_STRATEGY_ID = "v12_meta_gate"
_VERSION = "1.0.0"
_DEFAULT_GATE_THRESHOLD = 0.6
_DEFAULT_V12_UP = 0.70
_DEFAULT_V12_DOWN = 0.30


def evaluate_v12_meta_gate(surface: "FullDataSurface") -> StrategyDecision:
    p_v12 = getattr(surface, "probability_lgb_v12", None)
    p_gate = getattr(surface, "probability_v12_meta_gate", None)

    if p_v12 is None:
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
            skip_reason="v12_not_loaded",
            metadata={"probability_lgb_v12": None},
        )

    gate_threshold = _gp.get_float(
        "meta_gate_threshold", None, _DEFAULT_GATE_THRESHOLD
    )
    v12_up = _gp.get_float("v12_up_threshold", None, _DEFAULT_V12_UP)
    v12_down = _gp.get_float("v12_down_threshold", None, _DEFAULT_V12_DOWN)

    has_conviction = p_v12 >= v12_up or p_v12 <= v12_down
    if not has_conviction:
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
            skip_reason="v12_below_conviction",
            metadata={
                "probability_lgb_v12": p_v12,
                "probability_v12_meta_gate": p_gate,
            },
        )

    if p_gate is not None and p_gate < gate_threshold:
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
            skip_reason="v12_meta_gate_rejected",
            metadata={
                "probability_lgb_v12": p_v12,
                "probability_v12_meta_gate": p_gate,
            },
        )

    direction = "UP" if p_v12 >= 0.5 else "DOWN"
    confidence = "HIGH" if abs(p_v12 - 0.5) >= 0.20 else "MODERATE"
    confidence_score = float(abs(p_v12 - 0.5) * 2.0)

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=confidence,
        confidence_score=confidence_score,
        entry_cap=None,
        collateral_pct=None,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason="v12_meta_gate_pass",
        metadata={
            "probability_lgb_v12": p_v12,
            "probability_v12_meta_gate": p_gate,
        },
    )
