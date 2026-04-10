"""
Use case: Manage open positions — v4-aware exits + continuation.

Two phases of PR B layer on top of the PR #10 price/time exit logic:

1. **Event guard exits (EVENT_GUARD)**. When /v4/snapshot reports
   max_impact_in_window in {HIGH, EXTREME} and the event is within 2
   minutes, close every open position preemptively. The model isn't
   trained on release-day volatility spikes and the half-second before
   CPI is exactly when proprietary feeds start front-running.

2. **Cascade exhaustion exits (CASCADE_EXHAUSTED)**. When the 5m cascade
   FSM's exhaustion_t drops below 30s and the cascade direction matches
   our position's side, exit pre-emptively — the cascade is about to
   reverse and take our profit with it.

3. **Re-prediction continuation at is_expired()**. Instead of hard-exiting
   on MAX_HOLD_TIME, re-walk the entry gate stack using the cached v4
   snapshot. If all gates still pass, reset hold_clock_anchor to now and
   let the position continue into the next window. Otherwise exit with
   a specific reason code so telemetry separates WHICH gate killed the
   trade (probability flip vs regime deterioration vs macro flip vs
   consensus failure vs stale-data safety).

4. **Legacy v2 fallback**. When v4 snapshot is None (upstream hiccup,
   engine not running in v4 mode), fall back to the simpler force_refresh
   continuation on ProbabilityHttpAdapter. If even that fails, exit
   MAX_HOLD_TIME — the original PR #10 behaviour, fully recoverable.

Exit precedence (evaluated in order, first match wins):
  1. STOP_LOSS                   (price-based, unchanged)
  2. TAKE_PROFIT                 (price-based, unchanged)
  3. EVENT_GUARD                 (new, v4-aware)
  4. CASCADE_EXHAUSTED           (new, v4-aware)
  5. is_expired → _check_continuation:
       ├─ v4 available    → _continuation_v4 (6 gates)
       │                     ├─ all pass → extend hold clock, return None
       │                     └─ any fail → specific reason code
       └─ v4 unavailable  → _continuation_legacy_v2 (force_refresh + same-side check)
                             ├─ pass → extend hold clock
                             └─ fail → MAX_HOLD_TIME or PROBABILITY_REVERSAL
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from margin_engine.domain.entities.portfolio import Portfolio
from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import (
    AlertPort,
    ExchangePort,
    PositionRepository,
    ProbabilityPort,
    V4SnapshotPort,
)
from margin_engine.domain.value_objects import (
    ExitReason,
    Money,
    PositionState,
    Price,
    StopLevel,
    TradeSide,
    V4Snapshot,
)

logger = logging.getLogger(__name__)


class ManagePositionsUseCase:
    """
    Monitors open positions and closes them when exit conditions are met.

    Called from the main loop every tick. Evaluates all open positions
    against current price and signals.
    """

    def __init__(
        self,
        exchange: ExchangePort,
        portfolio: Portfolio,
        repository: PositionRepository,
        alerts: AlertPort,
        *,
        # ── PR B additions ──
        v4_snapshot_port: Optional[V4SnapshotPort] = None,
        probability_port: Optional[ProbabilityPort] = None,
        engine_use_v4_actions: bool = False,
        v4_primary_timescale: str = "15m",
        v4_timescales: tuple[str, ...] = ("5m", "15m", "1h", "4h"),
        v4_continuation_min_conviction: float = 0.10,
        v4_continuation_max: Optional[int] = None,
        v4_event_exit_seconds: int = 120,
        # ── PR #10 ──
        trailing_stop_pct: float = 0.003,   # 0.3%, matched to 0.6% SL
    ) -> None:
        self._exchange = exchange
        self._portfolio = portfolio
        self._repo = repository
        self._alerts = alerts
        self._v4_port = v4_snapshot_port
        self._probability_port = probability_port
        self._engine_use_v4_actions = engine_use_v4_actions
        self._v4_primary_timescale = v4_primary_timescale
        self._v4_timescales = v4_timescales
        self._v4_continuation_min_conviction = v4_continuation_min_conviction
        self._v4_continuation_max = v4_continuation_max
        self._v4_event_exit_seconds = v4_event_exit_seconds
        self._trailing_pct = trailing_stop_pct

    async def tick(self) -> list[Position]:
        """
        Check all open positions. Returns list of positions that were closed.

        Fetches the v4 snapshot ONCE per tick (not per position) so every
        open position sees the same consistent read. Falls back to None
        if the flag is off or the adapter is unavailable — _evaluate_exit
        degrades gracefully.
        """
        closed: list[Position] = []

        # Fetch v4 snapshot once per tick; shared across all positions.
        v4: Optional[V4Snapshot] = None
        if self._engine_use_v4_actions and self._v4_port is not None:
            try:
                v4 = await self._v4_port.get_latest(
                    asset="BTC", timescales=list(self._v4_timescales),
                )
            except Exception as e:
                logger.warning("manage_positions: v4 fetch failed: %s", e)

        for position in self._portfolio.open_positions:
            # Real close-side mark for this position — not the last-trade ticker
            mark = await self._exchange.get_mark(
                f"{position.asset}USDT", position.side,
            )
            exit_reason = await self._evaluate_exit(position, mark, v4)
            if exit_reason is not None:
                closed_pos = await self._close_position(position, exit_reason)
                if closed_pos:
                    closed.append(closed_pos)
            else:
                # Update trailing stop if price moved favourably
                self._update_trailing_stop(position, mark.value)

        return closed

    async def _evaluate_exit(
        self,
        position: Position,
        mark: Price,
        v4: Optional[V4Snapshot],
    ) -> Optional[ExitReason]:
        """
        Determine if a position should be closed and why.

        Exit precedence:
          1. STOP_LOSS        (price-based, unchanged)
          2. TAKE_PROFIT      (price-based, unchanged)
          3. EVENT_GUARD      (v4 only — HIGH/EXTREME event imminent)
          4. CASCADE_EXHAUSTED (v4 only — cascade FSM about to reverse)
          5. is_expired → _check_continuation
                           ├─ v4 available    → _continuation_v4
                           └─ v4 unavailable  → _continuation_legacy_v2
                                                 └─ probability_port None → MAX_HOLD_TIME
        """
        price = mark.value

        # ── 1. Stop-loss ──
        if position.should_stop_loss(price):
            return ExitReason.STOP_LOSS

        # ── 2. Take-profit ──
        if position.should_take_profit(price):
            return ExitReason.TAKE_PROFIT

        # ── 3. Event guard — forced exit 2 min before HIGH/EXTREME events ──
        # Only enforce when v4 is available and the flag is on. A stale
        # v4 snapshot missing this field is the same as "no event pending".
        if v4 is not None and v4.max_impact_in_window in ("HIGH", "EXTREME"):
            mtn = v4.minutes_to_next_high_impact
            if mtn is not None and (mtn * 60) < self._v4_event_exit_seconds:
                logger.info(
                    "v4 exit: EVENT_GUARD (%s in %.1f min) for position %s",
                    v4.max_impact_in_window, mtn, position.id,
                )
                return ExitReason.EVENT_GUARD

        # ── 4. Cascade exhaustion — cascade about to reverse on our side ──
        # Uses 5m cascade state (most reactive) regardless of the
        # position's own timescale — cascades move fast and a 15m
        # cascade-exhaustion read would be ~8 minutes stale.
        if v4 is not None:
            p5m = v4.timescales.get("5m")
            if (
                p5m is not None
                and p5m.cascade.exhaustion_t is not None
                and p5m.cascade.exhaustion_t < 30
            ):
                cascade_sig = p5m.cascade.signal or 0
                cascade_side = TradeSide.LONG if cascade_sig > 0 else TradeSide.SHORT
                if cascade_side == position.side:
                    logger.info(
                        "v4 exit: CASCADE_EXHAUSTED (t=%.1fs signal=%.2f) position %s",
                        p5m.cascade.exhaustion_t, cascade_sig, position.id,
                    )
                    return ExitReason.CASCADE_EXHAUSTED

        # ── 5. is_expired → continuation check (or legacy MAX_HOLD exit) ──
        if position.is_expired():
            return await self._check_continuation(position, v4)

        return None

    async def _check_continuation(
        self,
        position: Position,
        v4: Optional[V4Snapshot],
    ) -> Optional[ExitReason]:
        """
        Dispatch the continuation decision based on what data is available.

        Returns:
          - None if the position was continued (stay OPEN)
          - ExitReason on exit
        """
        # Hard cap on continuations (None = uncapped, per user's choice)
        if (
            self._v4_continuation_max is not None
            and position.continuation_count >= self._v4_continuation_max
        ):
            logger.info(
                "Position %s hit continuation cap (%d), exiting MAX_HOLD_TIME",
                position.id, self._v4_continuation_max,
            )
            return ExitReason.MAX_HOLD_TIME

        # v4 path preferred — richer gate stack
        if v4 is not None and self._engine_use_v4_actions:
            return await self._continuation_v4(position, v4)

        # Legacy v2 fallback — force_refresh on probability port
        if self._probability_port is not None:
            return await self._continuation_legacy_v2(position)

        # Neither path available — original v2 hard-exit behavior
        return ExitReason.MAX_HOLD_TIME

    async def _continuation_v4(
        self,
        position: Position,
        v4: V4Snapshot,
    ) -> Optional[ExitReason]:
        """
        Re-walk the entry gate stack at window close using the cached v4
        snapshot. If all gates still pass, extend the hold clock and
        return None so the position stays OPEN. Otherwise return a
        specific ExitReason so telemetry can attribute losses to the
        gate that killed each trade.

        Same gate order and semantics as OpenPositionUseCase._execute_v4,
        but with a looser conviction threshold (continuation_min_conviction,
        default 0.10) so once we're in the trade, any signal above random
        keeps us in it.
        """
        payload = v4.timescales.get(position.entry_timescale)
        if payload is None or not payload.is_tradeable:
            logger.info(
                "Position %s continuation: %s not tradeable (status=%s regime=%s), "
                "exiting PROBABILITY_REVERSAL",
                position.id, position.entry_timescale,
                payload.status if payload else "missing",
                payload.regime if payload else "?",
            )
            return ExitReason.PROBABILITY_REVERSAL

        # ── Consensus (infrastructure gate first) ──
        if not v4.consensus.safe_to_trade:
            logger.info(
                "Position %s continuation: consensus fail (%s), exiting CONSENSUS_FAIL",
                position.id, v4.consensus.safe_to_trade_reason,
            )
            return ExitReason.CONSENSUS_FAIL

        # ── Macro gate ──
        if v4.macro.status == "ok":
            if (
                v4.macro.direction_gate == "SKIP_UP"
                and position.side == TradeSide.LONG
            ):
                logger.info(
                    "Position %s continuation: macro flipped SKIP_UP, exiting",
                    position.id,
                )
                return ExitReason.MACRO_GATE_FLIP
            if (
                v4.macro.direction_gate == "SKIP_DOWN"
                and position.side == TradeSide.SHORT
            ):
                logger.info(
                    "Position %s continuation: macro flipped SKIP_DOWN, exiting",
                    position.id,
                )
                return ExitReason.MACRO_GATE_FLIP

        # ── Regime deteriorated ──
        if payload.regime in ("CHOPPY", "NO_EDGE"):
            logger.info(
                "Position %s continuation: regime=%s, exiting REGIME_DETERIORATED",
                position.id, payload.regime,
            )
            return ExitReason.REGIME_DETERIORATED

        # ── Probability flipped ──
        new_side = payload.suggested_side
        if new_side != position.side:
            logger.info(
                "Position %s continuation: probability flipped %s → %s (p_up=%.3f)",
                position.id, position.side.value, new_side.value,
                payload.probability_up or 0.0,
            )
            return ExitReason.PROBABILITY_REVERSAL

        # ── Conviction too weak for continuation ──
        # NB: this uses the looser continuation threshold (default 0.10)
        # not the entry threshold (default 0.10 too, but configurable
        # separately so they can diverge later).
        if not payload.meets_threshold(self._v4_continuation_min_conviction):
            logger.info(
                "Position %s continuation: conviction too weak "
                "(p_up=%.3f, needed |p-0.5|>=%.2f)",
                position.id, payload.probability_up or 0.0,
                self._v4_continuation_min_conviction,
            )
            return ExitReason.PROBABILITY_REVERSAL

        # ── ALL GATES PASS → CONTINUE ──
        now = time.time()
        position.continuation_count += 1
        position.last_continuation_ts = now
        position.last_continuation_p_up = payload.probability_up or 0.0
        position.hold_clock_anchor = now
        await self._repo.save(position)
        logger.info(
            "Position %s CONTINUED (#%d via v4): new p_up=%.3f regime=%s "
            "macro=%s consensus_safe=%s",
            position.id, position.continuation_count,
            payload.probability_up or 0.0, payload.regime,
            v4.macro.bias, v4.consensus.safe_to_trade,
        )
        return None  # stay OPEN

    async def _continuation_legacy_v2(
        self,
        position: Position,
    ) -> Optional[ExitReason]:
        """
        v2 fallback continuation: force_refresh the probability endpoint
        and check same-side + conviction. Used when v4 is unavailable.

        Simpler than _continuation_v4 — no regime/consensus/macro gates,
        just "does the model still agree with us?". If it does, extend
        the hold clock; if not or if the refresh fails, exit.
        """
        prob = await self._probability_port.force_refresh(
            asset="BTC", timescale=position.entry_timescale,
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
                position.id, position.side.value, prob.suggested_side.value,
                prob.probability_up,
            )
            return ExitReason.PROBABILITY_REVERSAL

        if not prob.meets_threshold(self._v4_continuation_min_conviction):
            logger.info(
                "Position %s legacy continuation: conviction too weak "
                "(p_up=%.3f, needed |p-0.5|>=%.2f)",
                position.id, prob.probability_up,
                self._v4_continuation_min_conviction,
            )
            return ExitReason.PROBABILITY_REVERSAL

        # ── CONTINUE via legacy path ──
        now = time.time()
        position.continuation_count += 1
        position.last_continuation_ts = now
        position.last_continuation_p_up = prob.probability_up
        position.hold_clock_anchor = now
        await self._repo.save(position)
        logger.info(
            "Position %s CONTINUED (#%d via v2 legacy): new p_up=%.3f",
            position.id, position.continuation_count, prob.probability_up,
        )
        return None  # stay OPEN

    async def _close_position(
        self,
        position: Position,
        reason: ExitReason,
    ) -> Optional[Position]:
        """Execute position close on exchange."""
        try:
            position.request_exit(reason)

            fill = await self._exchange.close_position(
                symbol=f"{position.asset}USDT",
                side=position.side,
                notional=position.notional,
            )

            position.confirm_exit(
                price=fill.fill_price,
                order_id=fill.order_id,
                commission=fill.commission,
                commission_is_actual=fill.commission_is_actual,
            )
            self._portfolio.on_position_closed(position)
            await self._repo.save(position)
            await self._alerts.send_trade_closed(position)

            logger.info(
                "Position closed: %s %s @ %.2f, PnL=%.2f, "
                "commission=%.4f (actual=%s), reason=%s",
                position.side.value, position.asset,
                fill.fill_price.value, position.realised_pnl,
                fill.commission, fill.commission_is_actual, reason.value,
            )
            return position

        except Exception as e:
            logger.error("Failed to close position %s: %s", position.id, e)
            # Revert state — position is still OPEN
            position.state = PositionState.OPEN
            position.exit_reason = None
            await self._alerts.send_error(f"Close failed for {position.id}: {e}")
            return None

    def _update_trailing_stop(self, position: Position, current_price: float) -> None:
        """Update trailing stop if price has moved favourably."""
        if not position.trailing_stop or not position.trailing_stop.is_trailing:
            return

        trail_pct = position.trailing_stop.trail_pct

        if position.side == TradeSide.LONG:
            new_stop = current_price * (1 - trail_pct)
            if new_stop > position.trailing_stop.price:
                position.trailing_stop = StopLevel(
                    price=new_stop,
                    is_trailing=True,
                    trail_pct=trail_pct,
                )
                # Also update the main stop_loss to the trailing level
                position.stop_loss = StopLevel(price=new_stop)
        else:
            new_stop = current_price * (1 + trail_pct)
            if new_stop < position.trailing_stop.price:
                position.trailing_stop = StopLevel(
                    price=new_stop,
                    is_trailing=True,
                    trail_pct=trail_pct,
                )
                position.stop_loss = StopLevel(price=new_stop)
