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
        *,
        is_secondary_fill: bool = False,
        parent_trade_id: Optional[str] = None,
    ) -> None:
        """Persist a completed trade.

        Steps:
          1. Register order with OrderManager (tracks fill lifecycle)
          2. Update window_snapshot trade_placed flag in DB

        ``is_secondary_fill`` / ``parent_trade_id`` (Hub #554, 2026-05-20):
          When the second on-chain fill lands inside the 25 s
          STALE_PLACEHOLDER_TTL window the use-case calls
          ``record_trade(..., is_secondary_fill=True,
          parent_trade_id=<primary order_id>)``. These flags are
          threaded into ``Order.metadata`` and propagate to the
          ``trades`` row via ``pg_trade_repo.write_trade`` (which
          mirrors ``metadata`` columns into dedicated columns).
        """
        if not result.success:
            return

        # ── Writer-bypass root-cause fix (2026-05-21, WRITER-BYPASS PR) ─────
        # PRIOR behaviour (incident 2026-04-17, Hub note #147): we returned
        # here for ``gtc_resting`` with ``fill_price=None`` to keep "phantom
        # trades" out of WR/P&L. That patched a downstream poisoning but
        # opened a much bigger hole: a GTC that LATER FILLS on-chain has
        # NO trade row to update. Wallet sees the debit, engine sees
        # nothing — disaster window 1779336900 (2026-05-21 04:18) leaked
        # ~$42.65 to a GTC fill that landed without ever producing a
        # ``trades`` row, because Step 7's recorder skipped it.
        #
        # NEW behaviour: ALWAYS record a row for a successful
        # ``execute_order``. For ``gtc_resting`` the row is provisional —
        # ``fill_price``/``fill_size`` start NULL and only get stamped
        # when the reconciler matches an on-chain fill against
        # ``clob_order_id``. Exposure queries already filter
        # ``COALESCE(fill_size, 0) > 0`` so the provisional row never
        # double-counts the cap; PR #561's ``v59_mark_phantom_trades``
        # migration sweeps any never-filled GTCs to ``status='PHANTOM'``
        # so WR/P&L stays clean. The net is: every on-chain fill now
        # has a corresponding ``trades`` row, no exception.
        #
        # We log the path so audit tooling can grep for ``gtc_resting``
        # provisional rows that later got matched / phantom-swept.
        if result.fill_price is None and result.execution_mode in ("gtc_resting", "gtc"):
            logger.info(
                "trade_recorder.gtc_resting_provisional_row",
                extra={
                    "order_id": result.order_id,
                    "execution_mode": result.execution_mode,
                    "note": (
                        "recording provisional row with NULL fill_price — "
                        "reconciler will stamp fill on-chain match or "
                        "phantom-sweep on window close"
                    ),
                },
            )

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
                        # Hub #554 sub-fill writer (2026-05-20). Default
                        # False — only the second-fill path through
                        # ExecuteTradeUseCase ever sets these to True.
                        "is_secondary_fill": bool(is_secondary_fill),
                        "parent_trade_id": parent_trade_id,
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
