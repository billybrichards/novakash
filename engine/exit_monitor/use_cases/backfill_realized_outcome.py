"""BackfillRealizedOutcomeUseCase — stamps realized P&L onto shadow trigger rows.

Called when a window resolves (WIN/LOSS/PUSH). Finds all exit_monitor_shadow
rows for that (decision_id or window_ts) and sets:
  - realized_outcome
  - realized_pnl_held_to_close  (what the trade earned/lost at settlement)
  - realized_pnl_shadow_exit    (what it would have earned at the CLOB bid that
                                  was captured at trigger time)

ev_delta is GENERATED ALWAYS AS (shadow_exit - held_to_close) so Postgres
computes it automatically on the same UPDATE.

Design note on pnl conventions:
  - realized_pnl_held_to_close = fill_price_net when WIN (+$0.15 on $0.85 entry
    means pnl = 0.15), negative for LOSS (-$0.85 on $0.85 entry).
  - realized_pnl_shadow_exit = clob_best_bid_held - entry_cost. When clob_best_bid_held
    is NULL (no CLOB data), this stays NULL too.
  - Caller supplies fill_price (entry cost) and outcome so we can compute both.
"""
from __future__ import annotations

from typing import Literal, Optional, Protocol

import structlog

log = structlog.get_logger(__name__)

Outcome = Literal["WIN", "LOSS", "PUSH"]


class ShadowExitBackfillRepo(Protocol):
    """Async update interface — implemented by ShadowExitGateway."""

    async def backfill_outcome(
        self,
        *,
        decision_id: int,
        realized_outcome: str,
        realized_pnl_held_to_close: float,
        realized_pnl_shadow_exit: Optional[float],
    ) -> int: ...  # returns row count updated


class BackfillRealizedOutcomeUseCase:
    """Updates exit_monitor_shadow rows once a window has resolved.

    Designed to be called from the window-resolution path — wherever the
    engine stamps WIN/LOSS onto strategy_decisions or trades rows. Pass the
    relevant decision_id + the fill_price used by that trade.
    """

    def __init__(self, repo: ShadowExitBackfillRepo) -> None:
        self._repo = repo

    async def execute(
        self,
        *,
        decision_id: int,
        outcome: Outcome,
        fill_price: float,          # entry cost per share (e.g. 0.85)
        clob_bid_at_trigger: Optional[float] = None,  # override from caller if available
    ) -> None:
        """Compute and write realized P&L fields for all shadow triggers.

        Args:
            decision_id:         strategy_decisions.id for the resolved trade.
            outcome:             'WIN' | 'LOSS' | 'PUSH' (Polymarket settlement).
            fill_price:          entry price paid per share (the cost basis).
            clob_bid_at_trigger: optional — if the caller has the CLOB bid stored
                                 separately, pass it here. Usually None; the repo
                                 reads clob_best_bid_held from the shadow row.
        """
        try:
            pnl_held = _pnl_held_to_close(outcome, fill_price)
            pnl_shadow = _pnl_shadow_exit(fill_price, clob_bid_at_trigger)

            n = await self._repo.backfill_outcome(
                decision_id=decision_id,
                realized_outcome=outcome,
                realized_pnl_held_to_close=pnl_held,
                realized_pnl_shadow_exit=pnl_shadow,
            )
            log.info(
                "exit_monitor.backfill_complete",
                decision_id=decision_id,
                outcome=outcome,
                pnl_held=f"{pnl_held:.4f}",
                pnl_shadow=f"{pnl_shadow:.4f}" if pnl_shadow is not None else "null",
                rows_updated=n,
            )
        except Exception as exc:
            log.warning(
                "exit_monitor.backfill_error",
                decision_id=decision_id,
                error=str(exc)[:300],
            )


# ── P&L helper functions ──────────────────────────────────────────────────────


def _pnl_held_to_close(outcome: str, fill_price: float) -> float:
    """Compute net P&L per share when held to settlement.

    Win:  receive $1.00 per share → net = 1.00 - fill_price
    Loss: receive $0.00 per share → net = 0.00 - fill_price = -fill_price
    Push: notional full refund    → net = 0.00 (neutral)
    """
    if outcome == "WIN":
        return round(1.0 - fill_price, 6)
    if outcome == "LOSS":
        return round(-fill_price, 6)
    return 0.0  # PUSH


def _pnl_shadow_exit(
    fill_price: float, clob_bid: Optional[float]
) -> Optional[float]:
    """Compute net P&L per share at the shadow-trigger CLOB best_bid.

    Returns None when no CLOB data is available — that's structural in
    Polymarket 5-minute markets where the bid side is a $0.01 stub.
    The table's ev_delta column will also be NULL in those rows.
    """
    if clob_bid is None:
        return None
    return round(clob_bid - fill_price, 6)
