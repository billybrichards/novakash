"""Use case: ReconcileOracleLossesUseCase — RDS note #614.

Problem
=======

The auto-redeem WIN path landed in PR #575 (``ReconcileRedemptionsUseCase``),
closing the gap where Polymarket's on-chain auto-redeem removed winning
positions from the data-api ``positions`` endpoint. But the LOSS-stamping
gap is still open:

  * Worthless tokens (curPrice = 0) **never produce an on-chain redemption
    transaction** — users don't redeem a losing token because the payout is
    zero — so the redeem-activity feed cannot see them.
  * The legacy ``ReconcilePositionsUseCase`` stamps LOSSes only when the
    position is visible in the data-api ``positions`` response. Some
    trades never make it there: trades whose CLOB POST returned no
    ``polymarket_order_id`` (network / parsing failures), or whose
    position record was vacuum-cleared by NegRisk auto-redeem of the WIN
    side of the same condition.

Net effect: trades sit ``outcome=NULL, status=OPEN, is_live=TRUE`` forever
even though Polymarket Gamma has long since resolved the window against
them. Audit RDS #614 found 9148, 9147, 9146, 9145, 9131, 9130, 9124,
9104, 9103 ... still NULL hours after their windows closed.

Solution
========

Symmetric counterpart of ``ReconcileRedemptionsUseCase``:

  1. Scan unresolved LIVE trades older than ``min_age_seconds`` (default
     30 min — Gamma resolution lag is ~4 min, plus margin).
  2. For each trade, look up ``window_snapshots.oracle_outcome`` via the
     trade's ``market_slug``.
  3. If oracle direction MISMATCHES the trade direction, stamp
     ``outcome='LOSS'`` with ``pnl_usd = -stake_usd``.
  4. If oracle direction MATCHES, defer (the redemption reconciler owns
     the WIN path — leaving the row NULL lets it stamp WIN with the
     correct payout from on-chain data).
  5. If oracle is not yet available, defer (will retry next pass).

Idempotency: ``stamp_oracle_loss`` UPDATE is guarded by ``WHERE outcome
IS NULL AND is_live = TRUE`` so re-running on the same trades is a
no-op. The redemption reconciler's WHERE guard (``outcome IS NULL AND
redeemed = FALSE``) similarly prevents the WIN path from re-stamping a
LOSS-stamped trade.

Non-conflict with WIN paths: this use case only writes LOSS. The
redemption reconciler only writes WIN. The legacy CLOB reconciler
writes both but only for trades visible in data-api positions (which
this use case complements by covering the worthless-token tail).
"""

from __future__ import annotations

import structlog

from domain.ports import TradeRepository, WindowStateRepository
from domain.value_objects import ReconcileOracleLossesResult

logger = structlog.get_logger(__name__)


# Trade direction → oracle direction the trade is BETTING ON.
# YES bets UP; wins iff oracle_outcome = UP.
# NO  bets DOWN; wins iff oracle_outcome = DOWN.
_BET_DIRECTION_FOR_TRADE = {"YES": "UP", "NO": "DOWN"}


class ReconcileOracleLossesUseCase:
    """Stamp ``outcome='LOSS'`` on unresolved live trades whose window has
    resolved against them.

    Wired into ``Orchestrator._sot_reconciler_loop`` as a fifth pass
    AFTER the existing reconcilers. Idempotent + disjoint from the WIN
    paths.

    Live-only — paper trades resolve via the oracle path inside
    ``ReconcilePositionsUseCase._resolve_paper_batch``.
    """

    def __init__(
        self,
        trade_repo: TradeRepository,
        window_state: WindowStateRepository,
        *,
        min_age_seconds: int = 1800,
    ) -> None:
        self._trade_repo = trade_repo
        self._window_state = window_state
        # Configurable so tests / backfills can use a smaller cutoff. Default
        # 30 min ensures the oracle has settled (~4 min Gamma lag + 26 min
        # margin for slow resolutions).
        self._min_age_seconds = int(min_age_seconds)

    async def execute(self) -> ReconcileOracleLossesResult:
        """Run one pass over unresolved live trades and stamp LOSSes.

        Returns counts so the orchestrator can log activity and surface to
        the dashboard.
        """
        trades_scanned = 0
        trades_stamped_loss = 0
        skipped_no_oracle = 0
        skipped_oracle_win = 0
        skipped_unparseable = 0
        errors = 0
        total_loss_usd = 0.0

        try:
            candidates = await self._trade_repo.find_unresolved_live_trades_older_than(
                self._min_age_seconds
            )
        except Exception as exc:
            logger.warning(
                "reconcile_oracle_losses.find_failed",
                error=str(exc)[:200],
            )
            return ReconcileOracleLossesResult(
                trades_scanned=0,
                trades_stamped_loss=0,
                skipped_no_oracle=0,
                skipped_oracle_win=0,
                skipped_unparseable=0,
                errors=1,
                total_loss_usd=0.0,
            )

        for trade in candidates:
            trades_scanned += 1
            try:
                slug = (trade.get("market_slug") or "").strip()
                direction = (trade.get("direction") or "").strip().upper()
                stake = trade.get("stake_usd")
                trade_id = trade.get("id")

                if not slug or direction not in _BET_DIRECTION_FOR_TRADE or stake is None or trade_id is None:
                    skipped_unparseable += 1
                    continue

                bet_direction = _BET_DIRECTION_FOR_TRADE[direction]

                oracle_outcome = await self._window_state.get_oracle_outcome_by_slug(
                    slug
                )
                if oracle_outcome not in ("UP", "DOWN"):
                    skipped_no_oracle += 1
                    continue

                if oracle_outcome == bet_direction:
                    # Trade WON — leave NULL so the redemption reconciler
                    # can stamp WIN with the correct on-chain payout.
                    skipped_oracle_win += 1
                    logger.debug(
                        "reconcile_oracle_losses.skip_win_defer_to_redeem",
                        trade_id=trade_id,
                        slug=slug[:60],
                        direction=direction,
                        oracle_outcome=oracle_outcome,
                    )
                    continue

                # Trade LOST — full stake is gone regardless of fill
                # quality, partial fills, or residual on-chain dust.
                pnl = -float(stake)
                stamped = await self._trade_repo.stamp_oracle_loss(
                    trade_id=int(trade_id),
                    pnl_usd=round(pnl, 4),
                )
                if stamped:
                    trades_stamped_loss += 1
                    total_loss_usd += pnl
                    logger.info(
                        "reconcile_oracle_losses.trade_stamped_loss",
                        trade_id=trade_id,
                        strategy=trade.get("strategy") or trade.get("strategy_id"),
                        slug=slug[:60],
                        direction=direction,
                        oracle_outcome=oracle_outcome,
                        stake_usd=round(float(stake), 4),
                        pnl_usd=round(pnl, 4),
                        execution_mode=trade.get("execution_mode"),
                        status_before=trade.get("status"),
                    )
                else:
                    # WHERE guard rejected — another pass already stamped
                    # this trade (e.g. legacy reconciler saw the position
                    # appear in data-api between our SELECT and UPDATE).
                    logger.debug(
                        "reconcile_oracle_losses.already_stamped",
                        trade_id=trade_id,
                        slug=slug[:60],
                    )
            except Exception as exc:
                errors += 1
                logger.warning(
                    "reconcile_oracle_losses.process_trade_failed",
                    trade_id=trade.get("id"),
                    slug=(trade.get("market_slug") or "")[:60],
                    error=str(exc)[:200],
                )

        return ReconcileOracleLossesResult(
            trades_scanned=trades_scanned,
            trades_stamped_loss=trades_stamped_loss,
            skipped_no_oracle=skipped_no_oracle,
            skipped_oracle_win=skipped_oracle_win,
            skipped_unparseable=skipped_unparseable,
            errors=errors,
            total_loss_usd=round(total_loss_usd, 4),
        )
