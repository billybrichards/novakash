"""Use case: Reconcile Positions.

Replaces: ``engine/reconciliation/reconciler.py::_resolve_position``
          (lines 710-930, ~215 LOC) and the orchestrator's
          ``sot_reconciler_loop`` scheduling (orchestrator.py L2192-2340).

Responsibility
--------------
Given a single resolved position from Polymarket (condition_id + outcome
data), match it to a trade row in the DB by token_id (exact -> prefix ->
cost fallback), compute PnL from per-trade data, and update the trade's
outcome/pnl_usd/resolved_at/status.

This use case is **not** wired into the orchestrator or reconciler yet.
It exists alongside the god class so the reconciler continues to call
``_resolve_position`` unchanged.  The wiring will happen in Phase 3.

Port dependencies (all from ``engine/domain/ports.py``):
  - TradeRepository -- find_by_token_id, find_by_token_prefix,
                       find_by_approximate_cost, resolve_trade
  - WindowStateRepository -- mark_resolved
  - AlerterPort -- resolution notifications
  - Clock -- deterministic time for testing
"""

from __future__ import annotations

import logging
from typing import Optional

from engine.domain.ports import (
    AlerterPort,
    Clock,
    TradeRepository,
    WindowStateRepository,
)
from engine.domain.value_objects import (
    PositionOutcome,
    ResolutionResult,
    WindowKey,
    WindowOutcome,
)

logger = logging.getLogger(__name__)


class ReconcilePositionsUseCase:
    """Resolve one Polymarket position against the trades table.

    Each call to :meth:`resolve_one` takes a single position's outcome
    data and attempts to match it to an unresolved trade row.  The
    three-tier matching strategy mirrors the existing reconciler logic:

      1. Exact token_id match
      2. Prefix token_id match (PE-02 workaround)
      3. Approximate cost match (last resort)

    The caller (reconciler loop or scheduler) decides how often to poll
    for new position outcomes and which ones to pass in.
    """

    def __init__(
        self,
        trade_repo: TradeRepository,
        window_state: WindowStateRepository,
        alerts: AlerterPort,
        clock: Clock,
    ) -> None:
        self._trade_repo = trade_repo
        self._window_state = window_state
        self._alerts = alerts
        self._clock = clock

    async def resolve_one(
        self,
        position: PositionOutcome,
    ) -> Optional[ResolutionResult]:
        """Resolve a single position outcome against the trades table."""
        outcome = position.outcome
        status = "RESOLVED_WIN" if outcome == "WIN" else "RESOLVED_LOSS"

        match, match_method = await self._find_matching_trade(position)

        if match is None:
            logger.warning(
                "reconciler.no_trade_match",
                extra={
                    "condition_id": position.condition_id[:20],
                    "token_id": position.token_id[:20] if position.token_id else "?",
                    "cost": f"${position.cost:.2f}",
                    "outcome": outcome,
                },
            )
            await self._notify_resolution(
                position=position,
                matched_trade_id=None,
                pnl=position.pnl_raw if outcome == "WIN" else -position.cost,
                shares=position.size,
                entry_price=position.avg_price,
                cost=position.cost,
            )
            return None

        # Compute PnL from per-trade data (not Polymarket aggregate)
        trade_id = match["id"]
        trade_stake = float(match.get("stake_usd") or position.cost)
        trade_entry = float(match.get("entry_price") or position.avg_price)
        trade_shares = (
            trade_stake / trade_entry if trade_entry > 0 else position.size
        )

        if outcome == "WIN":
            trade_pnl = round(trade_shares - trade_stake, 4)
        else:
            trade_pnl = round(-trade_stake, 4)

        await self._trade_repo.resolve_trade(
            trade_id=trade_id,
            outcome=outcome,
            pnl_usd=trade_pnl,
            status=status,
        )

        logger.info(
            "reconciler.trade_resolved",
            extra={
                "trade_id": trade_id,
                "token_id": (match.get("token_id") or "?")[:20],
                "outcome": outcome,
                "pnl": f"${trade_pnl:.2f}",
                "match_method": match_method,
            },
        )

        # Mark resolved in window state (non-fatal if this fails)
        try:
            asset = match.get("asset", "BTC")
            window_ts = match.get("window_ts")
            if window_ts:
                await self._window_state.mark_resolved(
                    WindowKey(asset=asset, window_ts=int(window_ts)),
                    WindowOutcome(
                        outcome=outcome,
                        pnl_usd=trade_pnl,
                        resolved_at=self._clock.now(),
                    ),
                )
        except Exception as exc:
            logger.debug(
                "reconciler.mark_resolved_failed",
                extra={"error": str(exc)[:100]},
            )

        await self._notify_resolution(
            position=position,
            matched_trade_id=trade_id,
            pnl=trade_pnl,
            shares=trade_shares,
            entry_price=trade_entry,
            cost=trade_stake,
        )

        return ResolutionResult(
            condition_id=position.condition_id,
            matched_trade_id=trade_id,
            outcome=outcome,
            pnl_usd=trade_pnl,
            status=status,
            token_id=match.get("token_id"),
            match_method=match_method,
        )

    async def _find_matching_trade(
        self,
        position: PositionOutcome,
    ) -> tuple[Optional[dict], Optional[str]]:
        """Three-tier matching strategy."""
        token_id = position.token_id

        # Tier 1: exact token_id match
        if token_id:
            match = await self._trade_repo.find_by_token_id(token_id)
            if match:
                return match, "exact"

        # Tier 2: prefix match (PE-02 workaround)
        if token_id and len(token_id) > 10:
            match = await self._trade_repo.find_by_token_prefix(token_id)
            if match:
                logger.info(
                    "reconciler.prefix_match",
                    extra={
                        "pos_token": token_id[:20],
                        "db_token": (match.get("token_id") or "")[:20],
                    },
                )
                return match, "prefix"

        # Tier 3: approximate cost match
        if position.cost > 0:
            match = await self._trade_repo.find_by_approximate_cost(
                position.cost,
            )
            if match:
                logger.info(
                    "reconciler.cost_fallback_match",
                    extra={
                        "trade_id": match["id"],
                        "cost": f"${position.cost:.2f}",
                        "condition_id": position.condition_id[:20],
                    },
                )
                return match, "cost_fallback"

        return None, None

    async def _notify_resolution(
        self,
        position: PositionOutcome,
        matched_trade_id: Optional[str],
        pnl: float,
        shares: float,
        entry_price: float,
        cost: float,
    ) -> None:
        """Send a Telegram notification for the resolution."""
        try:
            outcome = position.outcome
            pnl_str = f"+${pnl:.2f}" if outcome == "WIN" else f"${pnl:.2f}"
            emoji = "WIN" if outcome == "WIN" else "LOSS"
            source = "CLOB Reconciler" if matched_trade_id else "CLOB Reconciler (unmatched)"

            msg = (
                f"{emoji} -- Position Resolved\n"
                f"Condition: {position.condition_id[:20]}\n"
                f"Resolution: {outcome}\n"
                f"Shares: {shares:.2f} @ ${entry_price:.4f}\n"
                f"Cost: ${cost:.2f}\n"
                f"P&L: {pnl_str}\n"
                f"Source: {source}"
            )
            await self._alerts.send_system_alert(msg)
        except Exception:
            pass  # never let Telegram break reconciliation
