"""V10GateStrategy -- StrategyPort adapter wrapping the V10 gate pipeline.

Maps StrategyContext -> GateContext -> GatePipeline -> PipelineResult -> StrategyDecision.

This is a THIN ADAPTER.  It translates value objects, delegates to the
existing 8-gate pipeline in signals/gates.py, and translates back.
Zero business logic rewrite -- all trading logic lives in the gates.

Audit: SP-02.
"""
from __future__ import annotations

import traceback
from typing import Optional

import structlog

from domain.value_objects import StrategyContext, StrategyDecision
from signals.gates import (
    GateContext,
    GatePipeline,
    PipelineResult,
    EvalOffsetBoundsGate,
    SourceAgreementGate,
    DeltaMagnitudeGate,
    TakerFlowGate,
    CGConfirmationGate,
    DuneConfidenceGate,
    SpreadGate,
    DynamicCapGate,
)

log = structlog.get_logger(__name__)


class V10GateStrategy:
    """StrategyPort implementation wrapping the V10 gate pipeline.

    Maps StrategyContext -> GateContext -> GatePipeline ->
    PipelineResult -> StrategyDecision.
    """

    @property
    def strategy_id(self) -> str:  # noqa: D102
        return "v10_gate"

    @property
    def version(self) -> str:  # noqa: D102
        return "10.5.3"

    def __init__(self, *, dune_client=None):
        self._pipeline = GatePipeline([
            EvalOffsetBoundsGate(),
            SourceAgreementGate(),
            DeltaMagnitudeGate(),
            TakerFlowGate(),
            CGConfirmationGate(),
            DuneConfidenceGate(dune_client=dune_client),
            SpreadGate(),
            DynamicCapGate(),
        ])

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        """Evaluate the window via the V10 gate pipeline.

        MUST NOT raise -- all exceptions return an ERROR decision.
        """
        try:
            gate_ctx = self._build_gate_context(ctx)
            result: PipelineResult = await self._pipeline.evaluate(gate_ctx)
            return self._map_result(ctx, gate_ctx, result)
        except Exception as exc:
            log.warning("v10_gate.evaluate_error", error=str(exc)[:200])
            return StrategyDecision(
                action="ERROR",
                direction=None,
                confidence=None,
                confidence_score=None,
                entry_cap=None,
                collateral_pct=None,
                strategy_id=self.strategy_id,
                strategy_version=self.version,
                entry_reason="",
                skip_reason=f"v10_error: {str(exc)[:200]}",
                metadata={"traceback": traceback.format_exc()[-500:]},
            )

    def _build_gate_context(self, ctx: StrategyContext) -> GateContext:
        """Direct field mapping -- no logic, just translation."""
        from signals.v2_feature_body import build_v5_feature_body

        v5 = build_v5_feature_body(
            eval_offset=ctx.eval_offset,
            vpin=ctx.vpin,
            delta_pct=ctx.delta_pct,
            twap_delta=ctx.twap_delta,
            binance_price=ctx.current_price,
            tiingo_close=ctx.tiingo_close,
            delta_binance=ctx.delta_binance,
            delta_chainlink=ctx.delta_chainlink,
            delta_tiingo=ctx.delta_tiingo,
            regime=ctx.regime,
            delta_source=ctx.delta_source,
            prev_v2_probability_up=ctx.prev_dune_probability_up,
        )
        return GateContext(
            delta_chainlink=ctx.delta_chainlink,
            delta_tiingo=ctx.delta_tiingo,
            delta_binance=ctx.delta_binance,
            delta_pct=ctx.delta_pct,
            vpin=ctx.vpin,
            regime=ctx.regime,
            asset=ctx.asset,
            eval_offset=ctx.eval_offset,
            window_ts=ctx.window_ts,
            cg_snapshot=ctx.cg_snapshot,
            twap_delta=ctx.twap_delta,
            tiingo_close=ctx.tiingo_close,
            current_price=ctx.current_price,
            delta_source=ctx.delta_source,
            prev_v2_probability_up=ctx.prev_dune_probability_up,
            v5_features=v5,
            gamma_up_price=ctx.gamma_up_price,
            gamma_down_price=ctx.gamma_down_price,
        )

    def _map_result(
        self,
        ctx: StrategyContext,
        gate_ctx: GateContext,
        result: PipelineResult,
    ) -> StrategyDecision:
        """Map PipelineResult -> StrategyDecision."""
        gate_summaries = [
            {"gate": r.gate_name, "passed": r.passed, "reason": r.reason}
            for r in (result.gate_results or [])
        ]

        if result.passed:
            confidence = self._classify_confidence(gate_ctx, result)
            regime_tag = (ctx.regime or "UNKNOWN")[:10].upper()
            return StrategyDecision(
                action="TRADE",
                direction=result.direction,
                confidence=confidence,
                confidence_score=result.dune_p,
                entry_cap=result.cap or 0.65,
                collateral_pct=None,  # V10 uses fixed Kelly sizing
                strategy_id=self.strategy_id,
                strategy_version=self.version,
                entry_reason=f"v10_DUNE_{regime_tag}_T{ctx.eval_offset}",
                skip_reason=None,
                metadata={
                    "gate_results": gate_summaries,
                    "dune_p": result.dune_p,
                    "cg_modifier": gate_ctx.cg_threshold_modifier,
                },
            )
        else:
            return StrategyDecision(
                action="SKIP",
                direction=None,
                confidence=None,
                confidence_score=None,
                entry_cap=None,
                collateral_pct=None,
                strategy_id=self.strategy_id,
                strategy_version=self.version,
                entry_reason="",
                skip_reason=result.skip_reason or result.failed_gate or "v10 gate failed",
                metadata={"failed_gate": result.failed_gate, "gate_results": gate_summaries},
            )

    @staticmethod
    def _classify_confidence(
        gate_ctx: GateContext,
        result: PipelineResult,
    ) -> str:
        """Classify confidence from DUNE probability."""
        p = result.dune_p
        if p is not None and max(p, 1 - p) > 0.75:
            return "HIGH"
        return "MODERATE"
