"""Use case: ReconcileRedemptionsUseCase — Hub #586 / #591.

Problem
=======

Polymarket enabled on-chain auto-redeem in May 2026. Winning positions are
auto-settled to USDC and **removed from the data-api positions endpoint**.
The legacy ``ReconcilePositionsUseCase`` reads positions, classifies by
``curPrice`` (<=0.01 LOSS, >=0.99 WIN, else OPEN), and only stamps trades
that appear in the response. Auto-redeemed wins are invisible to it — they
stay ``outcome=NULL, status=OPEN, redeemed=false`` forever.

Losses (curPrice=0, redeemable=True) DO still appear in positions, so the
loss-stamping path keeps working. Result: dashboard shows 100 % loss-y
P&L while strategies are actually net positive. 2026-05-22 LIVE session
recorded -$184.98 in DB; Gamma verification showed +$28.52 actual.

Solution
========

Poll ``data-api.polymarket.com/activity?type=REDEEM&user=<funder>`` — the
authoritative ledger of on-chain auto-redeem transactions — and stamp the
matching trades.

For each ``RedemptionEvent``:
  1. Look up ``window_snapshots.oracle_outcome`` for the market_slug to
     determine the winning side (``"UP"`` or ``"DOWN"``).
  2. Map to trade ``direction``: ``"YES"`` won if outcome=UP, ``"NO"`` if
     outcome=DOWN.
  3. Find unresolved LIVE trades for that slug + direction.
  4. Atomically stamp ``outcome='WIN'``, ``payout_usd``, ``pnl_usd``,
     ``redeemed=true``, ``redemption_tx``, ``redeemed_at`` via a single
     UPDATE guarded by ``WHERE outcome IS NULL AND redeemed = FALSE``.

Idempotency: the WHERE guard means re-running the use case on the same
events is safe — the second pass updates zero rows. The
``find_unresolved_live_trades_for_slug`` query also filters
``outcome IS NULL`` so already-stamped trades aren't fetched.

Non-conflict with loss path: the existing reconciler only touches trades
when their condition_id appears in data-api positions. Losses do appear
there; redeemed wins do NOT. The two code paths are disjoint — this use
case stamps WINS only, the legacy path stamps LOSSES only.

Backfill mode: use ``BackfillRedemptionRunner`` (engine/scripts/) to
process the last N days of REDEEM events once, then exit.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

from domain.ports import TradeRepository, WindowStateRepository
from domain.value_objects import RedemptionEvent, ReconcileRedemptionsResult

logger = structlog.get_logger(__name__)


# Trade direction → Polymarket outcome the trade is BUYING.
# direction='YES' bets the window resolves UP; wins if oracle_outcome=UP.
# direction='NO' bets the window resolves DOWN; wins if oracle_outcome=DOWN.
_WIN_DIRECTION_FOR_OUTCOME = {"UP": "YES", "DOWN": "NO"}


class ReconcileRedemptionsUseCase:
    """Stamp auto-redeemed winners in the trades table.

    Wired into ``Orchestrator._sot_reconciler_loop`` as a fourth pass
    AFTER the existing ``ReconcilePositionsUseCase`` (which handles the
    loss path and any wins still visible in ``data-api positions``).
    Both passes are idempotent + disjoint, so the order does not matter.

    Live-only — paper trades resolve via the oracle path inside
    ``ReconcilePositionsUseCase._resolve_paper_batch``.
    """

    def __init__(
        self,
        trade_repo: TradeRepository,
        window_state: WindowStateRepository,
        *,
        alerter: Optional[Any] = None,
    ) -> None:
        self._trade_repo = trade_repo
        self._window_state = window_state
        self._alerter = alerter

    async def execute(
        self,
        events: list[RedemptionEvent],
    ) -> ReconcileRedemptionsResult:
        """Process a batch of REDEEM events, stamping the matching trades.

        ``events`` — list of RedemptionEvent from
        ``PolymarketRedemptionFeed.fetch_recent()``. Pass ``[]`` to no-op.
        """
        trades_stamped = 0
        events_skipped_no_match = 0
        events_skipped_no_oracle = 0
        errors = 0
        total_payout = 0.0

        for event in events:
            try:
                stamped_count, payout, skip_reason = await self._process_event(
                    event
                )
                trades_stamped += stamped_count
                total_payout += payout
                if skip_reason == "no_oracle":
                    events_skipped_no_oracle += 1
                elif skip_reason == "no_match":
                    events_skipped_no_match += 1
            except Exception as exc:
                errors += 1
                logger.warning(
                    "reconcile_redemptions.process_event_failed",
                    tx=event.transaction_hash[:20],
                    slug=event.market_slug[:60],
                    error=str(exc)[:200],
                )

        return ReconcileRedemptionsResult(
            events_seen=len(events),
            trades_stamped=trades_stamped,
            events_skipped_no_match=events_skipped_no_match,
            events_skipped_no_oracle=events_skipped_no_oracle,
            errors=errors,
            total_payout_usd=round(total_payout, 4),
        )

    async def _process_event(
        self,
        event: RedemptionEvent,
    ) -> tuple[int, float, Optional[str]]:
        """Stamp all unresolved WIN-side trades for one redemption event.

        Returns (stamped_count, total_payout_for_event, skip_reason).
        skip_reason is one of None | "no_oracle" | "no_match".
        """
        oracle_outcome = await self._window_state.get_oracle_outcome_by_slug(
            event.market_slug
        )
        if oracle_outcome not in ("UP", "DOWN"):
            logger.info(
                "reconcile_redemptions.skip_no_oracle",
                slug=event.market_slug[:60],
                tx=event.transaction_hash[:20],
                note="oracle_outcome not yet populated; will retry next pass",
            )
            return 0, 0.0, "no_oracle"

        winning_direction = _WIN_DIRECTION_FOR_OUTCOME[oracle_outcome]

        trades = await self._trade_repo.find_unresolved_live_trades_for_slug(
            event.market_slug,
            winning_direction=winning_direction,
        )

        if not trades:
            logger.info(
                "reconcile_redemptions.skip_no_match",
                slug=event.market_slug[:60],
                tx=event.transaction_hash[:20],
                winning_direction=winning_direction,
                oracle_outcome=oracle_outcome,
                usdc_size=event.usdc_size,
                note=(
                    "redemption has no unresolved trades — either already "
                    "stamped (idempotent re-run), or trades belong to a "
                    "different funder, or writer-bypass left zero rows"
                ),
            )
            return 0, 0.0, "no_match"

        stamped = 0
        payout_sum = 0.0
        for trade in trades:
            try:
                payout, pnl = self._compute_payout_and_pnl(trade)
            except Exception as exc:
                logger.warning(
                    "reconcile_redemptions.payout_calc_failed",
                    trade_id=trade.get("id"),
                    error=str(exc)[:120],
                )
                continue

            updated = await self._trade_repo.stamp_redemption_win(
                trade_id=int(trade["id"]),
                payout_usd=payout,
                pnl_usd=pnl,
                redemption_tx=event.transaction_hash,
                redeemed_at_epoch=int(event.timestamp),
            )
            if updated:
                stamped += 1
                payout_sum += payout
                logger.info(
                    "reconcile_redemptions.trade_stamped",
                    trade_id=trade["id"],
                    strategy=trade.get("strategy") or trade.get("strategy_id"),
                    slug=event.market_slug[:60],
                    direction=trade.get("direction"),
                    stake_usd=round(float(trade.get("stake_usd") or 0), 4),
                    payout_usd=round(payout, 4),
                    pnl_usd=round(pnl, 4),
                    redemption_tx=event.transaction_hash,
                    oracle_outcome=oracle_outcome,
                )
            else:
                # WHERE guard rejected — already stamped by a prior pass.
                # Common during a parallel-tick race between the legacy
                # ReconcilePositionsUseCase (cur_price>=0.99 path) and us.
                logger.debug(
                    "reconcile_redemptions.already_stamped",
                    trade_id=trade["id"],
                    slug=event.market_slug[:60],
                )

        return stamped, payout_sum, None

    @staticmethod
    def _compute_payout_and_pnl(trade: dict) -> tuple[float, float]:
        """Return (payout_usd, pnl_usd) for a winning trade.

        Mirrors ``ReconcilePositionsUseCase`` math so the two reconcilers
        agree on every redemption type.

        - Each winning share pays $1.00 USDC at resolution.
        - Prefer authoritative ``fill_size`` (CLOB shares received).
        - Derived fallback: ``stake / entry_price``. Last resort:
          ``stake / fill_price``.
        - ``pnl = payout - stake``.
        """
        stake = float(trade.get("stake_usd") or 0)
        entry = float(trade.get("entry_price") or 0)
        fill_price = trade.get("fill_price")
        raw_fill_size = trade.get("fill_size")

        if raw_fill_size is not None and float(raw_fill_size) > 0:
            shares = float(raw_fill_size)
        elif entry > 0:
            shares = stake / entry
        elif fill_price is not None and float(fill_price) > 0:
            shares = stake / float(fill_price)
        else:
            # No price info — defensive: treat shares = stake (i.e. 1.0
            # fill_price). This will UNDER-attribute the win; safe but
            # noisy.
            shares = stake

        payout = round(shares, 4)
        pnl = round(payout - stake, 4)
        return payout, pnl
