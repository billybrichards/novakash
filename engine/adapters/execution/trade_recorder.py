"""DB Trade Recorder -- persists executed trades.

Wraps DB writes that were previously scattered across five_min_vpin._execute_trade:
  - order_manager.register_order
  - db.update_window_trade_placed
  - metadata dict construction

Implements TradeRecorderPort from engine/domain/ports.py.

Audit: SP-06 Phase 4.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from use_cases.ports.execution import TradeRecorderPort
from domain.value_objects import (
    ExecutionResult,
    StakeCalculation,
    StrategyDecision,
)

logger = logging.getLogger(__name__)


class DBTradeRecorder(TradeRecorderPort):
    """Persist trade records to DB via the existing DBClient + OrderManager.

    All writes are defensive -- exceptions are caught and logged.
    The caller (ExecuteTradeUseCase) wraps this in try/except as well,
    so a DB failure never blocks the trade flow.
    """

    def __init__(
        self,
        db_client: Any = None,
        order_manager: Any = None,
    ) -> None:
        self._db = db_client
        self._om = order_manager

    async def record_trade(
        self,
        decision: StrategyDecision,
        result: ExecutionResult,
        stake: StakeCalculation,
    ) -> None:
        """Persist a completed trade.

        Steps:
          1. Register order with OrderManager (tracks fill lifecycle)
          2. Update window_snapshot trade_placed flag in DB
        """
        if not result.success:
            return

        # 1. Register with OrderManager
        if self._om is not None:
            try:
                from execution.order_manager import Order, OrderStatus

                # Pull regime / conviction out of decision.metadata (registry
                # populates them from the FullDataSurface on the TRADE path).
                # These flow into trades.metadata JSONB so the Trades UI can
                # surface REGIME / CONV columns per-row.
                decision_meta = decision.metadata or {}
                regime = decision_meta.get("regime")
                # dedup_key uniquely identifies the (strategy, window, direction)
                # triplet that the registry used for its in-memory dedup. Not
                # strictly enforced in DB but useful for triage (e.g. a pair
                # of trades with the same dedup_key indicates a re-entry bug).
                window_ts = decision_meta.get("window_ts")
                dedup_key = (
                    f"{decision.strategy_id}:{window_ts}:{decision.direction}"
                    if window_ts is not None
                    else None
                )

                order = Order(
                    order_id=result.order_id or f"unknown-{int(time.time())}",
                    strategy=decision.strategy_id,
                    venue="polymarket",
                    direction="NO" if decision.direction == "DOWN" else "YES",
                    price=str(result.fill_price or 0),
                    stake_usd=result.stake_usd,
                    fee_usd=result.fee_usd,
                    status=OrderStatus.OPEN,
                    btc_entry_price=0.0,  # Filled by caller context
                    window_seconds=300,
                    market_id=result.market_slug,
                    metadata={
                        "strategy_id": decision.strategy_id,
                        "strategy_version": decision.strategy_version,
                        "direction": decision.direction,
                        "confidence": decision.confidence,
                        "confidence_score": decision.confidence_score,
                        # `conviction` is the hub / FE name for the same
                        # HIGH/MEDIUM/LOW/NONE band; kept as a dedicated key
                        # so hub/api/trades.py's `_row_to_dict` can surface it
                        # without having to know about the `confidence` alias.
                        "conviction": decision.confidence,
                        "regime": regime,
                        "dedup_key": dedup_key,
                        "entry_reason": decision.entry_reason,
                        "entry_cap": decision.entry_cap,
                        "token_id": result.token_id,
                        "execution_mode": result.execution_mode,
                        "fak_attempts": result.fak_attempts,
                        "fak_prices": result.fak_prices,
                        "fill_price": result.fill_price,
                        "fill_size": result.fill_size,
                        "market_slug": result.market_slug,
                        "stake_bankroll": stake.bankroll,
                        "stake_fraction": stake.bet_fraction,
                        "stake_multiplier": stake.price_multiplier,
                        "engine_version": "registry_v2",
                    },
                )
                await self._om.register_order(order)
            except Exception as exc:
                logger.warning(
                    "trade_recorder.order_manager_error",
                    extra={"error": str(exc)[:200]},
                )

        # 2. Update window_snapshot in DB
        if self._db is not None:
            try:
                # Extract window_ts from market_slug
                parts = result.market_slug.split("-")
                window_ts = int(parts[-1]) if parts else 0
                asset = parts[0].upper() if parts else "BTC"
                timeframe = parts[2] if len(parts) >= 3 else "5m"

                await self._db.update_window_trade_placed(
                    window_ts=window_ts,
                    asset=asset,
                    timeframe=timeframe,
                )
            except Exception as exc:
                logger.warning(
                    "trade_recorder.db_update_error",
                    extra={"error": str(exc)[:200]},
                )
