"""
Position expiry management.

Handles position expiry checks and continuation logic including:
- Event guard exits (preemptive close before high-impact events)
- Cascade exhaustion exits (preemptive close before cascade reversal)
- Continuation checks (v4 and legacy v2 paths)
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

from margin_engine.domain.value_objects import ExitReason, TradeSide

if TYPE_CHECKING:
    from margin_engine.domain.entities.portfolio import Position
    from margin_engine.domain.ports import (
        PositionRepository,
        V4SnapshotPort,
        ProbabilityPort,
    )
    from margin_engine.adapters.signal.v4_models import V4Snapshot


logger = logging.getLogger(__name__)


class PositionExpiryManager:
    """
    Manages position expiry and continuation decisions.
    """

    def __init__(
        self,
        repository: "PositionRepository",
        *,
        v4_snapshot_port: Optional["V4SnapshotPort"] = None,
        probability_port: Optional["ProbabilityPort"] = None,
        engine_use_v4_actions: bool = False,
        v4_timescales: tuple[str, ...] = ("5m", "15m", "1h", "4h"),
        v4_continuation_min_conviction: float = 0.10,
        v4_continuation_max: Optional[int] = None,
        v4_event_exit_seconds: int = 120,
        v4_macro_mode: str = "advisory",
        v4_macro_hard_veto_confidence_floor: int = 80,
    ) -> None:
        self._repo = repository
        self._v4_port = v4_snapshot_port
        self._probability_port = probability_port
        self._engine_use_v4_actions = engine_use_v4_actions
        self._v4_timescales = v4_timescales
        self._v4_continuation_min_conviction = v4_continuation_min_conviction
        self._v4_continuation_max = v4_continuation_max
        self._v4_event_exit_seconds = v4_event_exit_seconds

        if v4_macro_mode not in ("veto", "advisory"):
            raise ValueError(
                f"v4_macro_mode must be 'veto' or 'advisory', got {v4_macro_mode!r}"
            )
        self._macro_mode = v4_macro_mode
        self._macro_hard_veto_confidence_floor = v4_macro_hard_veto_confidence_floor

    async def check_event_guard_exit(
        self,
        position: "Position",
        v4: Optional["V4Snapshot"],
    ) -> Optional[ExitReason]:
        """
        Check for event guard exit (preemptive close before high-impact event).

        Returns:
            ExitReason.EVENT_GUARD if exit should occur, None otherwise
        """
        if v4 is None:
            return None

        if v4.max_impact_in_window not in ("HIGH", "EXTREME"):
            return None

        mtn = v4.minutes_to_next_high_impact
        if mtn is None or (mtn * 60) >= self._v4_event_exit_seconds:
            return None

        logger.info(
            "v4 exit: EVENT_GUARD (%s in %.1f min) for position %s",
            v4.max_impact_in_window,
            mtn,
            position.id,
        )
        return ExitReason.EVENT_GUARD

    async def check_cascade_exhausted_exit(
        self,
        position: "Position",
        v4: Optional["V4Snapshot"],
    ) -> Optional[ExitReason]:
        """
        Check for cascade exhaustion exit (preemptive close before cascade reversal).

        Returns:
            ExitReason.CASCADE_EXHAUSTED if exit should occur, None otherwise
        """
        if v4 is None:
            return None

        p5m = v4.timescales.get("5m")
        if (
            p5m is None
            or p5m.cascade.exhaustion_t is None
            or p5m.cascade.exhaustion_t >= 30
        ):
            return None

        cascade_sig = p5m.cascade.signal or 0
        cascade_side = TradeSide.LONG if cascade_sig > 0 else TradeSide.SHORT

        if cascade_side != position.side:
            return None

        logger.info(
            "v4 exit: CASCADE_EXHAUSTED (t=%.1fs signal=%.2f) position %s",
            p5m.cascade.exhaustion_t,
            cascade_sig,
            position.id,
        )
        return ExitReason.CASCADE_EXHAUSTED

    async def check_continuation(
        self,
        position: "Position",
        v4: Optional["V4Snapshot"],
    ) -> Optional[ExitReason]:
        """
        Check if position should continue or expire.

        Returns:
            None if position should continue, ExitReason if should expire
        """
        # Hard cap on continuations
        if (
            self._v4_continuation_max is not None
            and position.continuation_count >= self._v4_continuation_max
        ):
            logger.info(
                "Position %s hit continuation cap (%d), exiting MAX_HOLD_TIME",
                position.id,
                self._v4_continuation_max,
            )
            return ExitReason.MAX_HOLD_TIME

        # v4 path preferred
        if v4 is not None and self._engine_use_v4_actions:
            return await self._continuation_v4(position, v4)

        # Legacy v2 fallback
        if self._probability_port is not None:
            return await self._continuation_legacy_v2(position)

        # Neither path available
        return ExitReason.MAX_HOLD_TIME

    async def _continuation_v4(
        self,
        position: "Position",
        v4: "V4Snapshot",
    ) -> Optional[ExitReason]:
        """
        V4 continuation check using full gate stack.

        Returns:
            None if position should continue, ExitReason if should expire
        """
        payload = v4.timescales.get(position.entry_timescale)
        if payload is None or not payload.is_tradeable:
            logger.info(
                "Position %s continuation: %s not tradeable (status=%s regime=%s), "
                "exiting PROBABILITY_REVERSAL",
                position.id,
                position.entry_timescale,
                payload.status if payload else "missing",
                payload.regime if payload else "?",
            )
            return ExitReason.PROBABILITY_REVERSAL

        # Consensus gate
        if not v4.consensus.safe_to_trade:
            logger.info(
                "Position %s continuation: consensus fail (%s), exiting CONSENSUS_FAIL",
                position.id,
                v4.consensus.safe_to_trade_reason,
            )
            return ExitReason.CONSENSUS_FAIL

        # Macro gate (veto mode only)
        if (
            v4.macro.status == "ok"
            and v4.macro.confidence >= self._macro_hard_veto_confidence_floor
            and self._macro_mode == "veto"
        ):
            if v4.macro.direction_gate == "SKIP_UP" and position.side == TradeSide.LONG:
                logger.info(
                    "Position %s continuation: macro flipped SKIP_UP "
                    "(confidence=%d, mode=veto), exiting MACRO_GATE_FLIP",
                    position.id,
                    v4.macro.confidence,
                )
                return ExitReason.MACRO_GATE_FLIP
            if (
                v4.macro.direction_gate == "SKIP_DOWN"
                and position.side == TradeSide.SHORT
            ):
                logger.info(
                    "Position %s continuation: macro flipped SKIP_DOWN "
                    "(confidence=%d, mode=veto), exiting MACRO_GATE_FLIP",
                    position.id,
                    v4.macro.confidence,
                )
                return ExitReason.MACRO_GATE_FLIP
        elif (
            v4.macro.status == "ok"
            and v4.macro.confidence >= self._macro_hard_veto_confidence_floor
            and self._macro_mode == "advisory"
            and (
                (
                    v4.macro.direction_gate == "SKIP_UP"
                    and position.side == TradeSide.LONG
                )
                or (
                    v4.macro.direction_gate == "SKIP_DOWN"
                    and position.side == TradeSide.SHORT
                )
            )
        ):
            logger.info(
                "Position %s continuation: macro advisory conflict "
                "(mode=advisory, macro=%s/%d/%s, side=%s) — NOT exiting",
                position.id,
                v4.macro.bias,
                v4.macro.confidence,
                v4.macro.direction_gate,
                position.side.value,
            )

        # Regime check
        if payload.regime in ("CHOPPY", "NO_EDGE"):
            logger.info(
                "Position %s continuation: regime=%s, exiting REGIME_DETERIORATED",
                position.id,
                payload.regime,
            )
            return ExitReason.REGIME_DETERIORATED

        # Probability flip check
        new_side = payload.suggested_side
        if new_side != position.side:
            logger.info(
                "Position %s continuation: probability flipped %s → %s (p_up=%.3f)",
                position.id,
                position.side.value,
                new_side.value,
                payload.probability_up or 0.0,
            )
            return ExitReason.PROBABILITY_REVERSAL

        # Conviction check
        if not payload.meets_threshold(self._v4_continuation_min_conviction):
            logger.info(
                "Position %s continuation: conviction too weak "
                "(p_up=%.3f, needed |p-0.5|>=%.2f)",
                position.id,
                payload.probability_up or 0.0,
                self._v4_continuation_min_conviction,
            )
            return ExitReason.PROBABILITY_REVERSAL

        # All gates pass - continue
        now = time.time()
        position.continuation_count += 1
        position.last_continuation_ts = now
        position.last_continuation_p_up = payload.probability_up or 0.0
        position.hold_clock_anchor = now
        await self._repo.save(position)
        logger.info(
            "Position %s CONTINUED (#%d via v4): new p_up=%.3f regime=%s "
            "macro=%s consensus_safe=%s",
            position.id,
            position.continuation_count,
            payload.probability_up or 0.0,
            payload.regime,
            v4.macro.bias,
            v4.consensus.safe_to_trade,
        )
        return None

    async def _continuation_legacy_v2(
        self,
        position: "Position",
    ) -> Optional[ExitReason]:
        """
        Legacy v2 continuation check.

        Returns:
            None if position should continue, ExitReason if should expire
        """
        prob = await self._probability_port.force_refresh(
            asset="BTC",
            timescale=position.entry_timescale,
        )
        if prob is None:
            logger.info(
                "Position %s legacy continuation: no fresh probability "
                "(stale/failed), exiting PROBABILITY_REVERSAL",
                position.id,
            )
            return ExitReason.PROBABILITY_REVERSAL

        if prob.suggested_side != position.side:
            logger.info(
                "Position %s legacy continuation: signal flipped %s → %s (p_up=%.3f)",
                position.id,
                position.side.value,
                prob.suggested_side.value,
                prob.probability_up,
            )
            return ExitReason.PROBABILITY_REVERSAL

        if not prob.meets_threshold(self._v4_continuation_min_conviction):
            logger.info(
                "Position %s legacy continuation: conviction too weak "
                "(p_up=%.3f, needed |p-0.5|>=%.2f)",
                position.id,
                prob.probability_up,
                self._v4_continuation_min_conviction,
            )
            return ExitReason.PROBABILITY_REVERSAL

        # Continue via legacy path
        now = time.time()
        position.continuation_count += 1
        position.last_continuation_ts = now
        position.last_continuation_p_up = prob.probability_up
        position.hold_clock_anchor = now
        await self._repo.save(position)
        logger.info(
            "Position %s CONTINUED (#%d via v2 legacy): new p_up=%.3f",
            position.id,
            position.continuation_count,
            prob.probability_up,
        )
        return None
