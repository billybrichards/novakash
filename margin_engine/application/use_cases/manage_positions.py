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
    TradeSide,
    V4Snapshot,
)
from margin_engine.application.use_cases.position_management import (
    StopLossManager,
    TakeProfitManager,
    TrailingStopManager,
    PositionExpiryManager,
)
from ..dto import ManagePositionsInput, ManagePositionsOutput

logger = logging.getLogger(__name__)


class ManagePositionsUseCase:
    """
    Monitors open positions and closes them when exit conditions are met.

    Called from the main loop every tick. Evaluates all open positions
    against current price and signals.
    """

    def __init__(
        self,
        input: ManagePositionsInput,
    ) -> None:
        self._input = input
        self._exchange = input.exchange
        self._portfolio = input.portfolio
        self._repo = input.repository
        self._alerts = input.alerts

        # Delegate managers
        self._stop_loss = StopLossManager()
        self._take_profit = TakeProfitManager()
        self._trailing = TrailingStopManager(default_trail_pct=input.trailing_stop_pct)
        self._expiry = PositionExpiryManager(
            repository=input.repository,
            v4_snapshot_port=input.v4_snapshot_port,
            probability_port=input.probability_port,
            engine_use_v4_actions=input.engine_use_v4_actions,
            v4_timescales=input.v4_timescales,
            v4_continuation_min_conviction=input.v4_continuation_min_conviction,
            v4_continuation_max=input.v4_continuation_max,
            v4_event_exit_seconds=input.v4_event_exit_seconds,
            v4_macro_mode=input.v4_macro_mode,
            v4_macro_hard_veto_confidence_floor=input.v4_macro_hard_veto_confidence_floor,
        )

    async def tick(self) -> ManagePositionsOutput:
        """
        Check all open positions. Returns output with closed positions and actions.

        Fetches the v4 snapshot ONCE per tick (not per position) so every
        open position sees the same consistent read. Falls back to None
        if the flag is off or the adapter is unavailable — _evaluate_exit
        degrades gracefully.
        """
        closed: list[Position] = []
        actions: list[str] = []

        v4: Optional[V4Snapshot] = None
        if self._expiry._v4_port is not None:
            try:
                v4 = await self._expiry._v4_port.get_latest(
                    asset="BTC",
                    timescales=list(self._expiry._v4_timescales),
                )
            except Exception as e:
                logger.warning("manage_positions: v4 fetch failed: %s", e)

        for position in self._portfolio.open_positions:
            mark = await self._exchange.get_mark(
                f"{position.asset}USDT",
                position.side,
            )
            exit_reason = await self._evaluate_exit(position, mark, v4)
            if exit_reason is not None:
                closed_pos = await self._close_position(position, exit_reason)
                if closed_pos:
                    closed.append(closed_pos)
                    actions.append(f"closed:{position.id}:{exit_reason.value}")
                else:
                    actions.append(f"close_failed:{position.id}")
            else:
                self._trailing.update_trailing_stop(position, mark.value)
                actions.append(f"continue:{position.id}")

        return ManagePositionsOutput(
            closed_positions=closed,
            actions_taken=actions,
            v4_snapshot=v4,
        )

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
        """
        price = mark.value

        if await self._stop_loss.check_stop_loss(position, mark):
            return ExitReason.STOP_LOSS

        if await self._take_profit.check_take_profit(position, mark):
            return ExitReason.TAKE_PROFIT

        event_guard = await self._expiry.check_event_guard_exit(position, v4)
        if event_guard is not None:
            return event_guard

        cascade_exhausted = await self._expiry.check_cascade_exhausted_exit(
            position, v4
        )
        if cascade_exhausted is not None:
            return cascade_exhausted

        if position.is_expired():
            return await self._expiry.check_continuation(position, v4)

        return None

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
                position.side.value,
                position.asset,
                fill.fill_price.value,
                position.realised_pnl,
                fill.commission,
                fill.commission_is_actual,
                reason.value,
            )
            return position

        except Exception as e:
            logger.error("Failed to close position %s: %s", position.id, e)
            position.state = PositionState.OPEN
            position.exit_reason = None
            await self._alerts.send_error(f"Close failed for {position.id}: {e}")
            return None
