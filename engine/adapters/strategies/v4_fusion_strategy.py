"""V4FusionStrategy -- StrategyPort adapter using the V4 fusion surface.

Consumes V4Snapshot (pre-fetched and attached to StrategyContext.v4_snapshot)
and applies conviction + regime rules to decide trade/skip.

Dynamic entry timing: The EvaluateStrategiesUseCase calls this at every
eval_offset (T-180 to T-5).  The strategy itself decides whether conditions
at this offset are good enough to trade.  It can return SKIP at T-180 and
TRADE at T-120 if conditions improve.  The dedup in
WindowStateRepository.was_traded() prevents double execution.

Audit: SP-03.
"""
from __future__ import annotations

import traceback
from typing import Optional

import structlog

from engine.domain.value_objects import StrategyContext, StrategyDecision, V4Snapshot

log = structlog.get_logger(__name__)


class V4FusionStrategy:
    """StrategyPort implementation using the V4 fusion surface."""

    # Conviction -> minimum probability_up distance from 0.5
    _CONVICTION_THRESHOLDS = {
        "HIGH":   0.12,    # P(UP) >= 0.62 or <= 0.38
        "MEDIUM": 0.15,    # P(UP) >= 0.65 or <= 0.35
        "LOW":    0.20,    # P(UP) >= 0.70 or <= 0.30
        "NONE":   1.0,     # Never trade
    }

    # Regime gating: which regimes are tradeable
    _TRADEABLE_REGIMES = {"calm_trend", "volatile_trend"}

    @property
    def strategy_id(self) -> str:  # noqa: D102
        return "v4_fusion"

    @property
    def version(self) -> str:  # noqa: D102
        return "4.0.0"

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        """Evaluate the window using V4 fusion snapshot.

        MUST NOT raise -- all exceptions return an ERROR decision.
        """
        try:
            return self._evaluate_inner(ctx)
        except Exception as exc:
            log.warning("v4_fusion.evaluate_error", error=str(exc)[:200])
            return self._error(f"v4_exception: {str(exc)[:200]}")

    def _evaluate_inner(self, ctx: StrategyContext) -> StrategyDecision:
        """Core evaluation logic -- 5 gates."""
        snap = ctx.v4_snapshot
        if snap is None:
            return self._error("v4_snapshot_missing")

        # Gate 1: Regime must be tradeable
        if snap.regime not in self._TRADEABLE_REGIMES:
            return self._skip(f"regime={snap.regime} not tradeable")

        # Gate 2: Consensus safe_to_trade
        if not snap.consensus.get("safe_to_trade", False):
            return self._skip("consensus not safe_to_trade")

        # Gate 3: Conviction threshold
        p_up = snap.probability_up
        distance = abs(p_up - 0.5)
        min_distance = self._CONVICTION_THRESHOLDS.get(snap.conviction, 1.0)
        if distance < min_distance:
            return self._skip(
                f"conviction={snap.conviction} requires distance={min_distance:.2f}, "
                f"got {distance:.2f} (p_up={p_up:.3f})"
            )

        # Gate 4: Direction from recommended_action
        direction = snap.recommended_side
        if direction is None:
            direction = "UP" if p_up > 0.5 else "DOWN"

        # Gate 5: Macro direction_gate
        macro_gate = snap.macro.get("direction_gate")
        if macro_gate is not None and macro_gate != direction:
            return self._skip(f"macro direction_gate={macro_gate} vs {direction}")

        # Sizing from V4 recommendation
        collateral_pct = snap.recommended_collateral_pct
        if collateral_pct is not None:
            size_modifier = snap.macro.get("size_modifier", 1.0)
            collateral_pct = collateral_pct * size_modifier

        return StrategyDecision(
            action="TRADE",
            direction=direction,
            confidence=snap.conviction,
            confidence_score=snap.conviction_score,
            entry_cap=None,             # V4 uses its own sizing, not V10 caps
            collateral_pct=collateral_pct,
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            entry_reason=self._build_reason(snap, ctx),
            skip_reason=None,
            metadata={
                "probability_up": p_up,
                "conviction": snap.conviction,
                "conviction_score": snap.conviction_score,
                "regime": snap.regime,
                "regime_confidence": snap.regime_confidence,
                "recommended_action": {
                    "side": snap.recommended_side,
                    "collateral_pct": snap.recommended_collateral_pct,
                    "sl_pct": snap.recommended_sl_pct,
                    "tp_pct": snap.recommended_tp_pct,
                    "reason": snap.recommended_reason,
                },
                "sub_signals": snap.sub_signals,
                "macro": snap.macro,
                "quantiles": snap.quantiles,
            },
        )

    def _build_reason(self, snap: V4Snapshot, ctx: StrategyContext) -> str:
        """Build a human-readable entry reason."""
        return (
            f"v4_{snap.conviction}_{snap.regime}_"
            f"T{ctx.eval_offset}_p{snap.probability_up:.2f}"
        )

    def _skip(self, reason: str) -> StrategyDecision:
        """Return a SKIP decision."""
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
            skip_reason=reason,
            metadata={},
        )

    def _error(self, reason: str) -> StrategyDecision:
        """Return an ERROR decision."""
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
            skip_reason=reason,
            metadata={},
        )
