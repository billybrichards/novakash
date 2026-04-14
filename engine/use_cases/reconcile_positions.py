"""Use case: Reconcile Positions.

Replaces: ``engine/reconciliation/reconciler.py::_resolve_position``
          (lines 710-930, ~215 LOC) and the orchestrator's
          ``sot_reconciler_loop`` scheduling (orchestrator.py L2192-2340).

Responsibility
--------------
Resolves both live and paper trades in a single pass via ``execute()``:

  - Live trades: given a ``PositionOutcome`` from the Polymarket CLOB API,
    match to a trade row by token_id (exact -> prefix -> cost fallback),
    compute PnL, and update outcome/pnl_usd/resolved_at/status.

  - Paper trades: scan unresolved paper trades older than 6 minutes,
    look up ``window_snapshots.actual_direction`` (Chainlink oracle),
    compare against trade direction to determine WIN/LOSS, update row.

Wired into ``Orchestrator._sot_reconciler_loop`` as a third pass after
the two SOT passes. Called every 2 minutes. Live path gated on
``ENGINE_USE_RECONCILE_UC=true`` + ``not paper_mode``.

Port dependencies (all from ``engine/domain/ports.py``):
  - TradeRepository -- find_by_token_id, find_by_token_prefix,
                       find_by_approximate_cost, resolve_trade,
                       find_unresolved_paper_trades
  - WindowStateRepository -- mark_resolved, get_actual_direction
  - AlerterPort -- resolution notifications
  - Clock -- deterministic time for testing
"""

from __future__ import annotations

import structlog
from typing import Optional

from domain.ports import (
    AlerterPort,
    Clock,
    TradeRepository,
    WindowStateRepository,
)
from domain.value_objects import (
    PositionOutcome,
    ReconcileResult,
    ResolutionResult,
    WindowKey,
    WindowOutcome,
)

logger = structlog.get_logger(__name__)


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

    async def execute(
        self,
        positions: list[PositionOutcome],
    ) -> ReconcileResult:
        """Run live (CLOB) and paper (oracle) resolution in a single pass.

        ``positions`` — list of PositionOutcome from poly_client.get_position_outcomes().
        Pass ``[]`` in paper mode (no real CLOB positions exist).
        """
        live_resolved = 0
        errors = 0

        for pos in positions:
            try:
                result = await self.resolve_one(pos)
                if result:
                    live_resolved += 1
            except Exception as exc:
                errors += 1
                logger.warning("reconciler.live_resolve_error", condition_id=pos.condition_id[:20], error=str(exc)[:100])

        # Shadow-label pass FIRST: stamp actual_direction on ALL resolved windows
        # so paper trades can look up their outcome.
        windows_labeled = 0
        try:
            windows_labeled = await self._window_state.label_resolved_windows()
        except Exception as exc:
            logger.warning("reconciler.label_windows_error", error=str(exc)[:100])

        paper_resolved, paper_skipped, paper_errors = await self._resolve_paper_batch()

        return ReconcileResult(
            live_resolved=live_resolved,
            paper_resolved=paper_resolved,
            paper_skipped=paper_skipped,
            errors=errors + paper_errors,
            windows_labeled=windows_labeled,
        )

    async def _resolve_paper_batch(self) -> tuple[int, int, int]:
        """Resolve all unresolved paper trades using oracle data.

        Returns (resolved_count, skipped_count, error_count).
        Skipped means the window hasn't resolved yet — will be retried next tick.
        """
        trades = await self._trade_repo.find_unresolved_paper_trades()
        resolved = 0
        skipped = 0
        errors = 0

        for trade in trades:
            try:
                raw_ts = trade.get("window_ts")
                asset = trade.get("asset") or "BTC"
                direction = (trade.get("direction") or "").upper()

                if not raw_ts or not direction:
                    skipped += 1
                    continue

                key = WindowKey(asset=asset, window_ts=int(raw_ts))
                actual_direction = await self._window_state.get_actual_direction(key)

                if actual_direction is None:
                    skipped += 1
                    continue

                # direction is "NO" (bet DOWN) or "YES" (bet UP) from Polymarket
                # actual_direction is "DOWN" or "UP" from window_snapshots
                trade_expects = "DOWN" if direction == "NO" else "UP"
                outcome = "WIN" if trade_expects == actual_direction.upper() else "LOSS"
                status = "RESOLVED_WIN" if outcome == "WIN" else "RESOLVED_LOSS"

                stake = float(trade.get("stake_usd") or 0)
                entry = float(trade.get("entry_price") or 0)
                shares = stake / entry if entry > 0 else 0.0
                pnl = round(shares - stake, 4) if outcome == "WIN" else round(-stake, 4)

                trade_id = trade["id"]
                await self._trade_repo.resolve_trade(
                    trade_id=trade_id,
                    outcome=outcome,
                    pnl_usd=pnl,
                    status=status,
                )

                logger.info(
                    "reconciler.paper_trade_resolved",
                    extra={
                        "trade_id": trade_id,
                        "direction": direction,
                        "actual_direction": actual_direction,
                        "outcome": outcome,
                        "pnl": f"${pnl:.2f}",
                    },
                )

                try:
                    emoji = "✅" if outcome == "WIN" else "❌"
                    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                    strategy_name = trade.get("strategy") or "unknown"
                    # Compute BTC delta direction from actual vs trade direction
                    btc_dir = actual_direction.upper()  # what BTC actually did
                    # e.g. trade direction DOWN, actual DOWN → BTC fell = "BTC closed ↓"
                    btc_arrow = "↓" if btc_dir == "DOWN" else "↑"
                    await self._alerts.send_system_alert(
                        f"{emoji} {outcome} {pnl_str} | {strategy_name} {direction} | BTC closed {btc_arrow}"
                    )
                except Exception:
                    pass  # never let Telegram break reconciliation

                resolved += 1

            except Exception as exc:
                errors += 1
                logger.warning("reconciler.paper_resolve_error", trade_id=trade.get("id"), error=str(exc)[:100])

        return resolved, skipped, errors

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
            logger.debug("reconciler.mark_resolved_failed", error=str(exc)[:100])

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
                logger.info("reconciler.prefix_match", pos_token=token_id[:20], db_token=(match.get("token_id") or "")[:20])
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
        """Send a compact Telegram notification for a live position resolution."""
        try:
            outcome = position.outcome
            emoji = "✅" if outcome == "WIN" else "❌"
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            match_note = "" if matched_trade_id else " (unmatched)"
            msg = f"{emoji} {outcome} {pnl_str} | LIVE{match_note} | cost ${cost:.2f}"
            await self._alerts.send_system_alert(msg)
        except Exception:
            pass  # never let Telegram break reconciliation
