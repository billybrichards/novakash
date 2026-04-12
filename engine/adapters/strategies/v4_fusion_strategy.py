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

from domain.value_objects import StrategyContext, StrategyDecision, V4Snapshot

log = structlog.get_logger(__name__)


class V4FusionStrategy:
    """StrategyPort implementation using the V4 fusion surface."""

    # Conviction -> minimum probability_up distance from 0.5
    # Legacy fallback -- used only when recommended_action lacks Polymarket
    # venue fields (i.e. old timesfm builds without polymarket_5m template).
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
        """Core evaluation logic.

        Two paths:
          1. Polymarket venue path: when the recommended_action has
             venue="polymarket" and a `trade` boolean, use its
             confidence_distance-based gating directly. This bypasses
             the legacy conviction threshold table.
          2. Legacy path (5 gates): for non-Polymarket or old timesfm
             builds that don't emit venue-aware recommendations.
        """
        snap = ctx.v4_snapshot
        if snap is None:
            return self._error("v4_snapshot_missing")

        # ── Polymarket venue-aware path ─────────────────────────────
        # The V4 surface provides a clean `polymarket_live_recommended_outcome`
        # block with direction, trade_advised, confidence metrics, and extras.
        poly = snap.polymarket_outcome
        if poly is not None:
            return self._evaluate_polymarket_v2(snap, ctx, poly)

        # Fallback: detect from recommended_action extras (old timesfm builds)
        rec_extras = self._get_rec_extras(snap)
        if rec_extras.get("venue") == "polymarket":
            return self._evaluate_polymarket(snap, ctx, rec_extras)

        # ── Legacy path (margin-engine templates) ───────────────────
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

        # Gate 5: Macro direction_gate (ALLOW_ALL = pass through)
        macro_gate = snap.macro.get("direction_gate")
        if macro_gate and macro_gate not in ("ALLOW_ALL", None):
            if macro_gate == "LONG_ONLY" and direction == "DOWN":
                return self._skip(f"macro direction_gate=LONG_ONLY blocks DOWN")
            if macro_gate == "SHORT_ONLY" and direction == "UP":
                return self._skip(f"macro direction_gate=SHORT_ONLY blocks UP")

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
            metadata=self._build_metadata(snap),
        )

    def _get_rec_extras(self, snap: V4Snapshot) -> dict:
        """Extract extras dict from the recommended_action metadata.

        The V4SnapshotHttpAdapter stores recommended_action fields on
        the V4Snapshot directly (recommended_side, recommended_reason,
        etc.) but the extras dict with venue info comes through the
        raw recommended_action.extras which the HTTP adapter doesn't
        currently parse. We read it from the raw recommendation stored
        in snap metadata if available, or detect venue from the reason.
        """
        # The recommended_reason contains venue info from the template
        reason = snap.recommended_reason or ""
        if "polymarket" in reason.lower():
            return {"venue": "polymarket", "trade": snap.recommended_side is not None}

        # Check if conviction_score and side match polymarket template pattern
        # Polymarket templates set side to UP/DOWN (not LONG/SHORT)
        if snap.recommended_side in ("UP", "DOWN"):
            return {
                "venue": "polymarket",
                "trade": snap.recommended_side is not None,
                "confidence_distance": abs(snap.probability_up - 0.5),
            }
        return {}

    def _evaluate_polymarket_v2(
        self, snap: V4Snapshot, ctx: StrategyContext, poly: dict
    ) -> StrategyDecision:
        """Evaluate using the clean polymarket_live_recommended_outcome block.

        This is the preferred path when the V4 surface provides the new
        venue-specific recommendation with direction, trade_advised,
        confidence metrics, timing, and extras.
        """
        direction = poly.get("direction")
        trade_advised = poly.get("trade_advised", False)
        confidence = poly.get("confidence", 0.5)
        distance = poly.get("confidence_distance", abs(confidence - 0.5))
        reason = poly.get("reason", "unknown")
        timing = poly.get("timing", "unknown")
        max_entry = poly.get("max_entry_price")

        if not trade_advised:
            return self._skip(f"polymarket: {reason} (timing={timing}, dist={distance:.3f})")

        if not direction:
            return self._skip("polymarket: no direction")

        # Macro direction_gate: ALLOW_ALL passes, LONG_ONLY/SHORT_ONLY filters
        macro_gate = snap.macro.get("direction_gate")
        if macro_gate and macro_gate not in ("ALLOW_ALL", None):
            if macro_gate == "LONG_ONLY" and direction == "DOWN":
                return self._skip(f"macro direction_gate=LONG_ONLY blocks DOWN")
            if macro_gate == "SHORT_ONLY" and direction == "UP":
                return self._skip(f"macro direction_gate=SHORT_ONLY blocks UP")

        return StrategyDecision(
            action="TRADE",
            direction=direction,
            confidence=snap.conviction or f"dist_{distance:.2f}",
            confidence_score=distance * 2.0,
            entry_cap=max_entry,
            collateral_pct=snap.recommended_collateral_pct,
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            entry_reason=f"polymarket_{reason}_T{ctx.eval_offset}",
            skip_reason=None,
            metadata={
                **self._build_metadata(snap),
                "polymarket_outcome": poly,
            },
        )

    def _evaluate_polymarket(
        self, snap: V4Snapshot, ctx: StrategyContext, extras: dict
    ) -> StrategyDecision:
        """Polymarket venue-aware evaluation.

        Trusts the template's trade decision. Uses confidence_distance
        (|p_up - 0.5|) as the primary signal instead of the legacy
        conviction tier thresholds.
        """
        p_up = snap.probability_up
        distance = extras.get("confidence_distance", abs(p_up - 0.5))
        trade = extras.get("trade", False)
        direction = snap.recommended_side or ("UP" if p_up > 0.5 else "DOWN")

        if not trade:
            reason = snap.recommended_reason or "polymarket_template_skip"
            return self._skip(f"polymarket: {reason} (dist={distance:.3f})")

        # Macro direction_gate: ALLOW_ALL = pass, LONG_ONLY/SHORT_ONLY = filter
        macro_gate = snap.macro.get("direction_gate")
        if macro_gate and macro_gate not in ("ALLOW_ALL", None):
            # LONG_ONLY blocks DOWN, SHORT_ONLY blocks UP
            if macro_gate == "LONG_ONLY" and direction == "DOWN":
                return self._skip(f"macro direction_gate=LONG_ONLY blocks DOWN")
            if macro_gate == "SHORT_ONLY" and direction == "UP":
                return self._skip(f"macro direction_gate=SHORT_ONLY blocks UP")

        # Use max_entry_price from extras if available
        max_entry = extras.get("max_entry_price")

        # Sizing from V4 recommendation
        collateral_pct = snap.recommended_collateral_pct
        if collateral_pct is not None:
            size_modifier = snap.macro.get("size_modifier", 1.0)
            collateral_pct = collateral_pct * size_modifier

        return StrategyDecision(
            action="TRADE",
            direction=direction,
            confidence=snap.conviction,
            confidence_score=snap.conviction_score or (distance * 2.0),
            entry_cap=max_entry,
            collateral_pct=collateral_pct,
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            entry_reason=self._build_reason(snap, ctx),
            skip_reason=None,
            metadata=self._build_metadata(snap),
        )

    def _build_metadata(self, snap: V4Snapshot) -> dict:
        """Build the metadata dict for a strategy decision."""
        return {
            "probability_up": snap.probability_up,
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
        }

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
