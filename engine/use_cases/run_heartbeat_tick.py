"""RunHeartbeatTickUseCase — one heartbeat tick (state sync, DB write).

Extracted from EngineRuntime._heartbeat_loop.
Responsibilities per tick:
  - Refresh EngineStateReaderAdapter cache (aggregator state + open orders)
  - Wallet balance refresh every 6 ticks (~60s), push into PublishHeartbeatUseCase
  - Delegate DB write (system_state heartbeat row + SITREP) to PublishHeartbeatUseCase

Out of scope (stays in runtime):
  - Feed connectivity writes (runtime owns feed objects and their .connected flags)
  - Mode sync (paper/live DB toggle)
  - Rich Telegram sitrep (400-line presentation layer)
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)


class RunHeartbeatTickUseCase:
    """Execute one heartbeat tick.

    Call execute() from the heartbeat loop every 10 seconds.
    State between calls (wallet counter, cached balance) is held internally.
    """

    def __init__(
        self,
        publish_heartbeat_uc: Any,
        engine_state_reader: Any,
        aggregator: Any,
        risk_manager: Any,
        order_manager: Optional[Any],
        poly_client: Optional[Any],
        settings: Any,
    ) -> None:
        self._publish_uc = publish_heartbeat_uc
        self._engine_state_reader = engine_state_reader
        self._aggregator = aggregator
        self._risk_manager = risk_manager
        self._order_manager = order_manager
        self._poly_client = poly_client
        self._settings = settings

        self._wallet_counter: int = 0
        self._cached_wallet_balance: Optional[float] = None

    async def execute(self) -> None:
        """Run one tick. Non-fatal errors are swallowed to keep the loop alive."""
        # Collect async state
        try:
            state = await self._aggregator.get_state()
        except Exception as exc:
            log.warning("heartbeat_tick.aggregator_error", error=str(exc)[:150])
            state = type("_EmptyState", (), {"vpin": None, "btc_price": None, "cascade": None})()

        open_positions = 0
        try:
            if self._order_manager:
                open_orders = await self._order_manager.get_open_orders()
                open_positions = len(open_orders)
        except Exception:
            pass

        # Refresh sync-property cache for PublishHeartbeatUseCase.
        # Feed connectivity is written separately by the runtime (owns feed objs).
        self._engine_state_reader.update(state, open_positions)

        # Wallet balance refresh every 6 ticks (~60s)
        self._wallet_counter += 1
        if self._wallet_counter >= 6:
            self._wallet_counter = 0
            if self._poly_client and not self._settings.paper_mode:
                try:
                    self._cached_wallet_balance = await self._poly_client.get_balance()
                    await self._risk_manager.sync_bankroll(self._cached_wallet_balance)
                except Exception as exc:
                    log.debug("heartbeat_tick.wallet_balance_error", error=str(exc))
            else:
                try:
                    risk_status = self._risk_manager.get_status()
                    self._cached_wallet_balance = risk_status.get("current_bankroll", 0)
                except Exception:
                    pass

        # Sync wallet balance into PublishHeartbeatUseCase so it lands in
        # system_state.config.wallet_balance_usdc and the SITREP payload.
        try:
            self._publish_uc.set_wallet_balance(self._cached_wallet_balance)
        except Exception:
            pass

        # Delegate DB write + SITREP to PublishHeartbeatUseCase
        try:
            await self._publish_uc.tick()
        except Exception as exc:
            log.error("heartbeat_tick.publish_error", error=str(exc)[:200])
