"""V4UpAsianStrategy -- Asian session UP-only variant of V4FusionStrategy.

Discovered 2026-04-12 via signal_evaluations analysis (5,543 samples, Apr 10-12):
  Gate:  v2_direction='UP' AND dist BETWEEN 0.15 AND 0.20 AND hour_utc IN (23,0,1,2)
  WR:    81-99% (Asian session medium-conviction UP signals)

Why it works:
  - Asian session (23:00-02:00 UTC) has lower liquidity and dominated by Asian retail
    accumulation — genuine UP pressure, not noise
  - Medium conviction band (dist 0.15-0.20) filters out weak signals while avoiding
    over-confident ones that have already been priced in by CLOB
  - UP predictions outside Asian session are near-random (50% WR) — time gate is critical

See docs/analysis/UP_STRATEGY_RESEARCH_BRIEF.md for full analysis.
Audit: SIG-05 (UP edge discovery).
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog

from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
from domain.value_objects import StrategyContext, StrategyDecision

log = structlog.get_logger(__name__)

# Asian session hours (UTC) — 23:00 to 02:59 UTC
_ASIAN_HOURS_UTC: frozenset[int] = frozenset({23, 0, 1, 2})

# Confidence distance band — medium conviction (0.15-0.20)
# Below 0.15: too weak, noise. Above 0.20: already priced in.
_MIN_DIST = 0.15
_MAX_DIST = 0.20

# Timing window (same as DOWN strategy, validated T-90-150)
_MIN_EVAL_OFFSET = 90
_MAX_EVAL_OFFSET = 150


class V4UpAsianStrategy(V4FusionStrategy):
    """V4 fusion surface: UP-only, Asian session, medium conviction."""

    @property
    def strategy_id(self) -> str:
        return "v4_up_asian"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        """Run V4 evaluation then apply Asian UP filter."""
        try:
            decision = await super().evaluate(ctx)
            return self._apply_asian_up(decision, ctx)
        except Exception as exc:
            log.warning("v4_up_asian.evaluate_error", error=str(exc)[:200])
            return self._error(f"v4_up_asian_exception: {str(exc)[:200]}")

    def _apply_asian_up(
        self, decision: StrategyDecision, ctx: StrategyContext
    ) -> StrategyDecision:
        """Post-process V4 decision: Asian UP filter."""
        if decision.action != "TRADE":
            return decision

        # Timing gate: T-90 to T-150 only
        offset = ctx.eval_offset
        if offset is not None and not (_MIN_EVAL_OFFSET <= offset <= _MAX_EVAL_OFFSET):
            return self._skip(
                f"asian_up_timing: T-{offset} outside T-{_MIN_EVAL_OFFSET}-T-{_MAX_EVAL_OFFSET}"
            )

        # Direction gate: UP only
        if decision.direction != "UP":
            return self._skip("asian_up_filter_down_skipped")

        # Confidence gate: medium conviction band 0.15-0.20
        p_up = ctx.v4_snapshot.probability_up if ctx.v4_snapshot else None
        dist = abs((p_up or 0.5) - 0.5) if p_up is not None else None
        if dist is None or not (_MIN_DIST <= dist <= _MAX_DIST):
            return self._skip(
                f"asian_up_conviction: dist={dist:.3f} outside [{_MIN_DIST},{_MAX_DIST}]"
                if dist is not None else "asian_up_conviction: no p_up"
            )

        # Time gate: Asian session only (23:00-02:59 UTC)
        hour_utc = self._current_hour_utc(ctx)
        if hour_utc not in _ASIAN_HOURS_UTC:
            return self._skip(
                f"asian_up_session: hour={hour_utc} not in Asian session {sorted(_ASIAN_HOURS_UTC)}"
            )

        return StrategyDecision(
            action=decision.action,
            direction=decision.direction,
            confidence=decision.confidence,
            confidence_score=decision.confidence_score,
            entry_cap=decision.entry_cap,
            collateral_pct=decision.collateral_pct,
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            entry_reason=f"{decision.entry_reason}_asian_up_h{hour_utc}_dist{dist:.2f}",
            skip_reason=None,
            metadata={
                **decision.metadata,
                "hour_utc": hour_utc,
                "dist": dist,
                "asian_session": True,
            },
        )

    def _current_hour_utc(self, ctx: StrategyContext) -> int:
        """Get current UTC hour from window_ts."""
        if ctx.window_ts:
            return datetime.fromtimestamp(ctx.window_ts, tz=timezone.utc).hour
        return datetime.now(tz=timezone.utc).hour
