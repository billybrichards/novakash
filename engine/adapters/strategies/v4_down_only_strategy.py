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


class V4DownOnlyStrategy(V4FusionStrategy):
    """V4 fusion surface with DOWN-only direction filter + CLOB sizing."""

    @property
    def strategy_id(self) -> str:
        return "v4_down_only"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        """Run V4 evaluation then apply DOWN-only filter + CLOB sizing."""
        try:
            decision = await super().evaluate(ctx)
            return self._apply_down_only(decision, ctx)
        except Exception as exc:
            log.warning("v4_down_only.evaluate_error", error=str(exc)[:200])
            return self._error(f"v4_down_only_exception: {str(exc)[:200]}")

    def _apply_down_only(
        self, decision: StrategyDecision, ctx: StrategyContext
    ) -> StrategyDecision:
        """Post-process V4 decision: filter UP, size DOWN by CLOB ask."""
        if decision.action != "TRADE":
            return decision

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
