"""ShadowExitGateway — async pg repository for exit_monitor_shadow table.

Implements both ShadowExitRepo (insert) and ShadowExitBackfillRepo (update).
Uses the engine's existing asyncpg pool pattern — pool resolved lazily
via db_client so it works before db.connect() completes at startup.

Never raises — all exceptions are caught and logged at WARNING so a DB
outage never cascades into the engine.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

from exit_monitor.domain.shadow_trigger import ShadowTrigger

log = structlog.get_logger(__name__)


class ShadowExitGateway:
    """Async pg gateway for exit_monitor_shadow.

    Parameters:
        db_pool:    asyncpg pool (may be None at init — resolved lazily).
        db_client:  engine DB client with a ``_pool`` attribute (fallback).
    """

    def __init__(
        self,
        db_pool: Any = None,
        db_client: Any = None,
    ) -> None:
        self._db_pool = db_pool
        self._db_client = db_client

    def _resolve_pool(self) -> Any:
        """Return live asyncpg pool, resolving lazily via db_client."""
        if self._db_pool is not None:
            return self._db_pool
        if self._db_client is not None:
            return getattr(self._db_client, "_pool", None)
        return None

    async def insert_trigger(
        self,
        trigger: ShadowTrigger,
        clob_best_bid_held: Optional[float],
        clob_best_ask_held: Optional[float],
        clob_best_bid_against: Optional[float],
        clob_best_ask_against: Optional[float],
        clob_book_depth_usd: Optional[float],
    ) -> None:
        """INSERT one shadow trigger row into exit_monitor_shadow.

        Swallows all errors — never crashes the monitor on DB failure.
        """
        pool = self._resolve_pool()
        if pool is None:
            log.debug("shadow_exit_gateway.no_pool", decision_id=trigger.decision_id)
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO exit_monitor_shadow (
                        decision_id, asset, window_ts, strategy_id, side,
                        entry_p, entry_eval_offset,
                        trigger_eval_offset, trigger_threshold,
                        p_against_at_trigger, p_for_at_trigger,
                        tickformer_model,
                        clob_best_bid_held, clob_best_ask_held,
                        clob_best_bid_against, clob_best_ask_against,
                        clob_book_depth_usd
                    ) VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7,
                        $8, $9,
                        $10, $11,
                        $12,
                        $13, $14,
                        $15, $16,
                        $17
                    )
                    """,
                    trigger.decision_id,
                    trigger.asset,
                    trigger.window_ts,
                    trigger.strategy_id,
                    trigger.side,
                    trigger.entry_p,
                    trigger.entry_eval_offset,
                    trigger.trigger_eval_offset,
                    trigger.threshold,
                    trigger.p_against,
                    trigger.p_for,
                    trigger.tickformer_model,
                    clob_best_bid_held,
                    clob_best_ask_held,
                    clob_best_bid_against,
                    clob_best_ask_against,
                    clob_book_depth_usd,
                )
        except Exception as exc:
            log.warning(
                "shadow_exit_gateway.insert_error",
                decision_id=trigger.decision_id,
                threshold=trigger.threshold,
                error=str(exc)[:200],
            )

    async def backfill_outcome(
        self,
        *,
        decision_id: int,
        realized_outcome: str,
        realized_pnl_held_to_close: float,
        realized_pnl_shadow_exit: Optional[float],
    ) -> int:
        """UPDATE shadow rows for a resolved decision_id.

        Sets realized_outcome, realized_pnl_held_to_close, and
        realized_pnl_shadow_exit on all rows for this decision_id where
        realized_outcome is still NULL (idempotent guard).

        When clob_best_bid_held was NULL at insert time and the caller
        doesn't supply realized_pnl_shadow_exit, we update ONLY the
        outcome + held_to_close so ev_delta remains NULL (honest — we
        don't have the exit price). If clob_best_bid_held was captured,
        the caller can pass it derived from the stored row.

        Returns the count of rows updated.
        """
        pool = self._resolve_pool()
        if pool is None:
            log.debug(
                "shadow_exit_gateway.no_pool_backfill", decision_id=decision_id
            )
            return 0
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE exit_monitor_shadow
                    SET
                        realized_outcome           = $2,
                        realized_pnl_held_to_close = $3,
                        realized_pnl_shadow_exit   = CASE
                            WHEN $4::numeric IS NOT NULL THEN $4::numeric
                            -- fallback: derive from stored CLOB bid if available
                            WHEN clob_best_bid_held IS NOT NULL THEN clob_best_bid_held - $5::numeric
                            ELSE NULL
                        END
                    WHERE decision_id = $1
                      AND realized_outcome IS NULL
                    """,
                    decision_id,
                    realized_outcome,
                    realized_pnl_held_to_close,
                    realized_pnl_shadow_exit,
                    realized_pnl_held_to_close + 1.0,  # fill_price = 1.0 - pnl_held (WIN) or -pnl_held (LOSS)
                )
                # asyncpg returns "UPDATE N" string
                n = int((result or "UPDATE 0").split()[-1])
                return n
        except Exception as exc:
            log.warning(
                "shadow_exit_gateway.backfill_error",
                decision_id=decision_id,
                error=str(exc)[:200],
            )
            return 0
