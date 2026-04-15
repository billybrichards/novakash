"""Use case: Execute Manual Trade.

Replaces: ``engine/strategies/orchestrator.py::_manual_trade_poller``
          (lines 2575-2810, ~235 LOC).

Responsibility
--------------
Drain the ``manual_trades`` table of pending rows, look up each row's
CLOB token_id (ring buffer -> DB fallback), and place the trade (or mark
``failed_no_token``).

This use case is **not** wired into the orchestrator yet.  It exists
alongside the god class so the orchestrator continues to run its own
``_manual_trade_poller`` loop unchanged.  The wiring will happen in
Phase 3 of the migration plan.

Port dependencies (all from ``engine/domain/ports.py``):
  - PolymarketClientPort -- place_order, poll_pending_trades, get_window_market
  - ManualTradeRepository -- update_status, get_token_ids (DB fallback for LT-02)
  - WindowStateRepository -- token_id ring buffer equivalent
  - AlerterPort -- LT-02 failure alerts + execution confirmations
  - Clock -- deterministic time for testing
"""

from __future__ import annotations

import logging
from typing import Optional

from domain.ports import (
    ManualTradeRepository,
    PolymarketClientPort,
    WindowStateRepository,
)
from use_cases.ports import AlerterPort, Clock
from domain.value_objects import (
    FillResult,
    ManualTradeOutcome,
    PendingTrade,
)

logger = logging.getLogger(__name__)


class ExecuteManualTradeUseCase:
    """Drain pending manual trades and execute them on the CLOB.

    Each call to :meth:`drain_once` polls for pending trades, resolves
    the CLOB token_id for each via a two-source fallback chain (ring
    buffer -> market_data DB), and either places the order or marks the
    trade as ``failed_no_token``.

    The use case does NOT own the polling loop or the sleep interval --
    the caller (orchestrator or a scheduler) decides how often to invoke
    ``drain_once()``.
    """

    def __init__(
        self,
        polymarket: PolymarketClientPort,
        manual_trade_repo: ManualTradeRepository,
        window_state: WindowStateRepository,
        alerts: AlerterPort,
        clock: Clock,
        *,
        paper_mode: bool = True,
        price_buffer: float = 0.02,
        max_price: float = 0.65,
    ) -> None:
        self._polymarket = polymarket
        self._manual_trade_repo = manual_trade_repo
        self._window_state = window_state
        self._alerts = alerts
        self._clock = clock
        self._paper_mode = paper_mode
        self._price_buffer = price_buffer
        self._max_price = max_price

    async def drain_once(self) -> list[ManualTradeOutcome]:
        """Poll pending trades and execute each one."""
        pending = await self._polymarket.poll_pending_trades()
        if not pending:
            return []

        outcomes: list[ManualTradeOutcome] = []
        for trade in pending:
            outcome = await self._execute_one(trade)
            outcomes.append(outcome)

        return outcomes

    async def _execute_one(self, trade: PendingTrade) -> ManualTradeOutcome:
        """Process a single pending manual trade."""
        trade_id = trade.trade_id
        direction = "YES" if trade.direction == "UP" else "NO"

        logger.info(
            "manual_trade.executing",
            extra={
                "trade_id": trade_id,
                "direction": direction,
                "entry_price": f"${trade.entry_price:.4f}",
                "stake": f"${trade.stake_usd:.2f}",
            },
        )

        # Step 1: mark executing
        await self._manual_trade_repo.update_status(trade_id, "executing")

        try:
            # Step 2: resolve token_id via fallback chain
            token_id, token_source = await self._resolve_token_id(
                trade, direction,
            )

            if not token_id:
                await self._manual_trade_repo.update_status(
                    trade_id, "failed_no_token",
                )
                await self._alert_token_failure(trade, direction)
                return ManualTradeOutcome(
                    trade_id=trade_id,
                    status="failed_no_token",
                    paper=self._paper_mode,
                )

            logger.info(
                "manual_trade.token_id_resolved",
                extra={"trade_id": trade_id, "source": token_source},
            )

            # Step 3: place order
            clob_order_id = await self._place_order(
                trade, direction, token_id,
            )

            # Step 4: mark open
            await self._manual_trade_repo.update_status(
                trade_id, "open", clob_order_id=clob_order_id,
            )

            # Step 5: alert success
            await self._alert_success(trade, direction)

            return ManualTradeOutcome(
                trade_id=trade_id,
                status="open",
                clob_order_id=clob_order_id,
                paper=self._paper_mode,
                token_source=token_source,
            )

        except Exception as exc:
            logger.error(
                "manual_trade.execution_failed",
                extra={"trade_id": trade_id, "error": str(exc)},
            )
            error_status = f"failed: {str(exc)[:50]}"
            await self._manual_trade_repo.update_status(
                trade_id, error_status,
            )
            return ManualTradeOutcome(
                trade_id=trade_id,
                status=error_status,
                paper=self._paper_mode,
            )

    async def _resolve_token_id(
        self,
        trade: PendingTrade,
        direction: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Two-source fallback chain for CLOB token_id resolution.

        1. Primary: PolymarketClientPort.get_window_market()
        2. Fallback: ManualTradeRepository.get_token_ids() (LT-02 fix)
        """
        market = await self._polymarket.get_window_market(
            trade.asset, trade.window_ts,
        )
        if market is not None:
            token_id = (
                market.up_token_id if direction == "YES"
                else market.down_token_id
            )
            if token_id:
                return token_id, "window_market"

        logger.info(
            "manual_trade.market_miss_fetching_from_db",
            extra={
                "trade_id": trade.trade_id,
                "window_ts": trade.window_ts,
                "asset": trade.asset,
            },
        )
        md_row = await self._manual_trade_repo.get_token_ids(
            asset=trade.asset,
            window_ts=trade.window_ts,
            timeframe=trade.timeframe,
        )
        if md_row:
            token_id = (
                md_row.get("up_token_id") if direction == "YES"
                else md_row.get("down_token_id")
            )
            if token_id:
                return token_id, "market_data_db"

        return None, None

    async def _place_order(
        self,
        trade: PendingTrade,
        direction: str,
        token_id: str,
    ) -> Optional[str]:
        """Place the CLOB order (paper or live)."""
        if self._paper_mode:
            clob_id = f"manual-paper-{trade.trade_id[:12]}"
            logger.info(
                "manual_trade.paper_filled",
                extra={"trade_id": trade.trade_id, "clob_id": clob_id},
            )
            return clob_id

        capped_price = min(
            trade.entry_price + self._price_buffer,
            self._max_price,
        )
        result = await self._polymarket.place_order(
            token_id=token_id,
            side=direction,
            size=trade.stake_usd,
            price=round(capped_price, 4),
        )
        clob_id = result.order_id if isinstance(result, FillResult) else str(result)
        logger.info(
            "manual_trade.live_submitted",
            extra={
                "trade_id": trade.trade_id,
                "clob_id": str(clob_id)[:20],
            },
        )
        return clob_id

    async def _alert_token_failure(
        self, trade: PendingTrade, direction: str,
    ) -> None:
        """Send Telegram alert for LT-02 token_id resolution failure."""
        try:
            await self._alerts.send_system_alert(
                f"Manual Trade FAILED\n\n"
                f"Trade ID: {trade.trade_id[:16]}\n"
                f"Direction: {direction} ({trade.asset} {trade.timeframe})\n"
                f"Reason: no CLOB token_id found for "
                f"window_ts={trade.window_ts}\n"
                f"Tried: window_market + market_data_db\n\n"
                f"The window may be too stale or data-collector "
                f"hasn't written it yet. Try a fresh window.",
            )
        except Exception:
            pass

    async def _alert_success(
        self, trade: PendingTrade, direction: str,
    ) -> None:
        """Send Telegram alert for successful trade execution."""
        try:
            mode = "PAPER" if self._paper_mode else "LIVE"
            await self._alerts.send_system_alert(
                f"Manual Trade Executed ({mode})\n"
                f"Direction: {trade.direction}\n"
                f"Entry: ${trade.entry_price:.4f}\n"
                f"Stake: ${trade.stake_usd:.2f}\n"
                f"Trade ID: {trade.trade_id[:16]}",
            )
        except Exception:
            pass
