"""Backward-compat shim — real implementation in use_cases/gates/pipeline.py.

DO NOT add new code here. Import from use_cases.gates instead.
"""
from use_cases.gates.pipeline import *  # noqa: F401, F403
from use_cases.gates.pipeline import (  # explicit for IDEs / type checkers
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
