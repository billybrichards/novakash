"""Gate pipeline — use-case layer.

The 8-gate evaluation pipeline is application orchestration, not signal
calculation. Signal calculators (VPIN, cascade, regime) stay in signals/.
"""
from use_cases.gates.pipeline import (
    GatePipeline,
    GateContext,
    GateResult,
    PipelineResult,
    EvalOffsetBoundsGate,
    SourceAgreementGate,
    DeltaMagnitudeGate,
    TakerFlowGate,
    CGConfirmationGate,
    DuneConfidenceGate,
    SpreadGate,
    DynamicCapGate,
    CoinGlassVetoGate,
    GateContextDelta,
    EMPTY_DELTA,
)

__all__ = [
    "GatePipeline",
    "GateContext",
    "GateResult",
    "PipelineResult",
    "EvalOffsetBoundsGate",
    "SourceAgreementGate",
    "DeltaMagnitudeGate",
    "TakerFlowGate",
    "CGConfirmationGate",
    "DuneConfidenceGate",
    "SpreadGate",
    "DynamicCapGate",
    "CoinGlassVetoGate",
    "GateContextDelta",
    "EMPTY_DELTA",
]
