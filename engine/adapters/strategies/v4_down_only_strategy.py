"""V4DownOnlyStrategy -- DOWN-only variant of V4FusionStrategy.

Applies the DOWN-only filter discovered in 897K-sample analysis (2026-04-12):
  - DOWN predictions: 76–99% WR across all CLOB bands → always trade
  - UP predictions: 1.5–53% WR across all CLOB bands → always skip

CLOB-based sizing (applied to DOWN trades):
  clob_down_ask >= 0.75: 2.0× (market + model agree, 99% WR)
  clob_down_ask 0.55–0.75: 1.5× (strong agreement, 97% WR)
  clob_down_ask 0.35–0.55: 1.2× (mild agreement, 92% WR)
  clob_down_ask < 0.35: 1.0× (genuine contrarian, 76% WR)

See docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md for full analysis.
Audit: SIG-03, SIG-04.
"""
from __future__ import annotations

import structlog

from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
from domain.value_objects import StrategyContext, StrategyDecision

log = structlog.get_logger(__name__)

# CLOB sizing schedule — keyed on clob_down_ask thresholds
_CLOB_SIZING: list[tuple[float, float, str]] = [
    (0.75, 2.0, "double_confirm_99pct"),   # market + model agree
    (0.55, 1.5, "strong_98pct"),
    (0.35, 1.2, "mild_92pct"),
    (0.0,  1.0, "contrarian_76pct"),       # genuine contrarian
]

_MAX_COLLATERAL_PCT = 0.10   # cap: never bet more than 10% per trade

# Confidence threshold — relaxed from V4's default 0.12 to 0.10.
# 897K-sample analysis: DOWN WR at dist>=0.10 = 90.5% vs dist>=0.12 = 90.6%.
# Adds ~50K more trades at same WR. Validated 2026-04-12.
_MIN_CONFIDENCE_DIST = 0.10

# Timing window validated from 897K-sample analysis — T-90 to T-150 has 90.3% WR.
# Outside this band accuracy degrades to ~50-65%.
_MIN_EVAL_OFFSET = 90
_MAX_EVAL_OFFSET = 150


class V4DownOnlyStrategy(V4FusionStrategy):
    """V4 fusion surface with DOWN-only direction filter + CLOB sizing."""

    @property
    def strategy_id(self) -> str:
        return "v4_down_only"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        """Run V4 evaluation with relaxed confidence (0.10 vs parent's 0.12)."""
        try:
            decision = await super().evaluate(ctx)

            # If parent SKIPped on confidence < 0.12, re-check at our lower 0.10.
            # Parent skip format: "polymarket: p_up=X.XXX dist=X.XXX < 0.12 threshold"
            if (
                decision.action == "SKIP"
                and decision.skip_reason
                and "< 0.12 threshold" in decision.skip_reason
            ):
                snap = ctx.v4_snapshot
                if snap and snap.probability_up is not None:
                    dist = abs(snap.probability_up - 0.5)
                    if dist >= _MIN_CONFIDENCE_DIST:
                        # Re-derive direction and build TRADE decision
                        poly = snap.polymarket_outcome or {}
                        direction = poly.get("direction") or (
                            "DOWN" if snap.probability_up < 0.5 else "UP"
                        )
                        decision = StrategyDecision(
                            action="TRADE",
                            direction=direction,
                            confidence=snap.conviction or f"dist_{dist:.2f}",
                            confidence_score=dist * 2.0,
                            entry_cap=poly.get("max_entry_price"),
                            collateral_pct=snap.recommended_collateral_pct,
                            strategy_id=self.strategy_id,
                            strategy_version=self.version,
                            entry_reason=f"polymarket_down_only_dist{dist:.2f}_T{ctx.eval_offset}",
                            skip_reason=None,
                            metadata=self._build_metadata(snap) if hasattr(self, '_build_metadata') else {},
                        )

            return self._apply_down_only(decision, ctx)
        except Exception as exc:
            log.warning("v4_down_only.evaluate_error", error=str(exc)[:200])
            return self._error(f"v4_down_only_exception: {str(exc)[:200]}")

    def _apply_down_only(
        self, decision: StrategyDecision, ctx: StrategyContext
    ) -> StrategyDecision:
        """Post-process V4 decision: timing gate, UP filter, CLOB sizing."""
        if decision.action != "TRADE":
            return decision

        # Timing gate: only trade T-90 to T-150 (validated sweet spot, 90.3% WR)
        offset = ctx.eval_offset
        if offset is not None and not (_MIN_EVAL_OFFSET <= offset <= _MAX_EVAL_OFFSET):
            return self._skip(
                f"down_only_timing: T-{offset} outside T-{_MIN_EVAL_OFFSET} to T-{_MAX_EVAL_OFFSET}"
            )

        # Filter: skip all UP predictions
        if decision.direction == "UP":
            return self._skip("down_only_filter_up_skipped")

        # Apply CLOB-based sizing for DOWN trades
        size_mod, label = self._clob_size_modifier(ctx.clob_down_ask)

        base_pct = decision.collateral_pct or 0.025
        new_pct = min(base_pct * size_mod, _MAX_COLLATERAL_PCT)

        return StrategyDecision(
            action=decision.action,
            direction=decision.direction,
            confidence=decision.confidence,
            confidence_score=decision.confidence_score,
            entry_cap=decision.entry_cap,
            collateral_pct=new_pct,
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            entry_reason=f"{decision.entry_reason}_down_{label}",
            skip_reason=None,
            metadata={
                **decision.metadata,
                "clob_down_ask": ctx.clob_down_ask,
                "clob_size_modifier": size_mod,
                "clob_size_label": label,
            },
        )

    def _clob_size_modifier(
        self, clob_down_ask: float | None
    ) -> tuple[float, str]:
        """Return (size_modifier, label) based on clob_down_ask."""
        if clob_down_ask is None:
            return 1.0, "no_clob_data"
        for threshold, modifier, label in _CLOB_SIZING:
            if clob_down_ask >= threshold:
                return modifier, label
        return 1.0, "contrarian_76pct"
