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
        strategy_decision_repo: Any = None,
    ) -> None:
        self._db = db_client
        self._om = order_manager
        # Audit #255 F5 — post-execution write-back to strategy_decisions.
        # Optional: when None we skip the update silently.
        self._sd_repo = strategy_decision_repo

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

        # Reject phantom trades: success=True but no actual fill.
        # gtc_resting returns success=True with fill_price=None when the
        # order sits on the book unfilled. Recording these as trades
        # poisons WR/P&L calculations (incident 2026-04-17, Hub note #147).
        if result.fill_price is None and result.execution_mode in ("gtc_resting", "gtc"):
            logger.warning(
                "trade_recorder.phantom_rejected",
                extra={
                    "order_id": result.order_id,
                    "execution_mode": result.execution_mode,
                    "reason": "fill_price is None — refusing to record phantom trade",
                },
            )
            return

        # 1. Register with OrderManager
        if self._om is not None:
            try:
                from execution.order_manager import Order, OrderStatus

                # Parse via the DecisionMetadata VO — applies the
                # v4_regime/v4_conviction legacy-key fallback internally
                # (see engine/domain/decision_metadata.py). Phase 3 of the
                # Three Builders convergence: all new writes use canonical
                # ``regime`` / ``conviction`` keys directly via the
                # StrategyDecision factory, so the fallback only serves
                # historical rows — it has no effect on post-Phase-3b
                # decisions.
                from domain.decision_metadata import DecisionMetadata

                decision_vo = DecisionMetadata.from_dict(decision.metadata)
                regime = decision_vo.regime
                # dedup_key uniquely identifies the (strategy, window, direction)
                # triplet that the registry used for its in-memory dedup. Not
                # strictly enforced in DB but useful for triage (e.g. a pair
                # of trades with the same dedup_key indicates a re-entry bug).
                # Fall back to parsing from market_slug when window_ts is not
                # embedded in decision.metadata (edge cases where the VO
                # wasn't populated with window_ts).
                window_ts = decision_vo.window_ts
                if window_ts is None and result.market_slug:
                    try:
                        window_ts = int(result.market_slug.rsplit("-", 1)[-1])
                    except (ValueError, IndexError):
                        window_ts = None
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
                    fill_price=(
                        float(result.fill_price)
                        if result.fill_price is not None
                        else None
                    ),
                    stake_usd=result.stake_usd,
                    fee_usd=result.fee_usd,
                    status=OrderStatus.OPEN,
                    btc_entry_price=0.0,  # Filled by caller context
                    window_seconds=300,
                    market_id=result.market_slug,
                    metadata={
                        # Spread decision metadata first so signal-strength fields
                        # (probability_lgb, probability_classifier, pl_dist, pc_dist,
                        # disagreement, is_vhc, etc.) survive into trades.metadata.
                        # Execution-specific keys below overlay any same-named keys.
                        **(decision.metadata or {}),
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
                        # Sub-fill capture (2026-05-21, hub note #BLOCKING_v9_4):
                        # Persist the raw `transactionsHashes` / `tradeIDs`
                        # arrays from the CLOB FAK response so on-chain
                        # attribution + reconciliation can see every
                        # maker matched, not just the aggregate. Empty
                        # list = single-maker fill (or GTC/RFQ leg —
                        # those don't carry sub-fills today).
                        "sub_fill_tx_hashes": list(result.transactions_hashes),
                        "sub_fill_trade_ids": list(result.trade_ids),
                        "sub_fill_count": len(result.transactions_hashes),
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

        # 3. Audit #255 F5 — flag the strategy_decision row as executed and
        #    write the real fill details. Forward-only — historical rows
        #    remain NULL by design (no backfill). Defensive: a missing repo
        #    or raise here never blocks the trade flow.
        if self._sd_repo is not None:
            try:
                sd_window_ts: Optional[int] = None
                sd_asset: str = "BTC"
                parts = (result.market_slug or "").split("-") if result.market_slug else []
                if len(parts) >= 1 and parts[0]:
                    sd_asset = parts[0].upper()
                if parts:
                    try:
                        sd_window_ts = int(parts[-1])
                    except (ValueError, TypeError):
                        sd_window_ts = None
                if sd_window_ts is None:
                    # Metadata may carry window_ts directly (engine-side write).
                    meta_ts = (
                        decision.metadata.get("window_ts") if decision.metadata else None
                    )
                    if meta_ts is not None:
                        try:
                            sd_window_ts = int(meta_ts)
                        except (ValueError, TypeError):
                            sd_window_ts = None

                if sd_window_ts is not None:
                    await self._sd_repo.mark_executed(
                        strategy_id=decision.strategy_id,
                        asset=sd_asset,
                        window_ts=sd_window_ts,
                        order_id=result.order_id,
                        fill_price=result.fill_price,
                        fill_size=result.fill_size,
                    )
            except Exception as exc:
                logger.warning(
                    "trade_recorder.sd_mark_executed_error",
                    extra={"error": str(exc)[:200]},
                )
