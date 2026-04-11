"""
CLOB Position Reconciler — definitive source of truth.

Polls Polymarket every 2s for wallet balance and position outcomes.
Detects new resolutions, updates the trades table, and sends Telegram reports.

This is READ-ONLY from Polymarket's perspective — it never places orders.
All CLOB API calls go through PolymarketClient.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

import structlog

from reconciliation.state import (
    OpenPosition,
    ReconcilerState,
    RestingOrder,
    WalletSnapshot,
)

if TYPE_CHECKING:
    from alerts.telegram import TelegramAlerter
    from execution.polymarket_client import PolymarketClient, PolyOrderStatus  # noqa: F401

log = structlog.get_logger(__name__)


# ─── POLY-SOT result types ──────────────────────────────────────────────────
#
# Returned by `CLOBReconciler.reconcile_manual_trades_sot()` so callers (the
# orchestrator loop, tests, future status endpoints) can introspect what the
# pass actually did. Counts only — the per-row state is persisted to the DB
# in `manual_trades.sot_reconciliation_state`.

@dataclass
class ReconciliationSummary:
    """Outcome counts from a single SOT reconciliation pass."""
    checked: int = 0
    agrees: int = 0
    unreconciled: int = 0
    engine_optimistic: int = 0
    polymarket_only: int = 0
    diverged: int = 0
    skipped_no_order_id: int = 0
    # POLY-SOT-d: new terminal state for paper-mode rows. Paper trades are
    # never reconciled against Polymarket (they never touched the CLOB),
    # so the reconciler skips them cleanly instead of tagging them as
    # engine_optimistic. Heuristic: order_id starts with `5min-` /
    # `manual-paper-` or clob_order_id IS NULL AND order_id is not a 0x
    # hash.
    paper: int = 0
    errors: int = 0
    alerts_fired: int = 0
    rows: list[dict] = field(default_factory=list)


class CLOBReconciler:
    """
    Polls Polymarket CLOB every 2s to maintain a definitive view of:
    - Wallet balance (USDC)
    - Open positions and their outcomes
    - Resting GTC orders

    On new resolution: matches to trades by token_id, updates outcome/pnl.
    Every 5 minutes: sends a reconciliation report to Telegram.
    Every 60s: checks for orphaned GTC fills via CLOB trade history API.
    """

    def __init__(
        self,
        poly_client: PolymarketClient,
        db_pool,
        alerter: TelegramAlerter,
        shutdown_event: asyncio.Event,
        poll_interval: float = 2.0,
        report_interval: float = 300.0,
        sot_price_tolerance_pct: float = 0.5,
    ) -> None:
        self._poly = poly_client
        self._pool = db_pool
        self._alerter = alerter
        self._shutdown = shutdown_event
        self._poll_interval = poll_interval
        self._report_interval = report_interval
        # POLY-SOT: 0.5% default tolerance on price match. Anything outside
        # this band marks the row `diverged`.
        self._sot_price_tolerance_pct = float(sot_price_tolerance_pct)

        self._state = ReconcilerState()
        self._known_resolved: set[str] = set()

        # Tracking for 5-min report windows
        self._report_wins: list[float] = []
        self._report_losses: list[float] = []
        self._report_filled: int = 0
        self._report_expired: int = 0

        self._poll_task: Optional[asyncio.Task] = None
        self._report_task: Optional[asyncio.Task] = None

        # Orphan check runs every ~60s, not every 2s poll
        self._last_orphan_check: float = 0.0
        self._orphan_check_interval: float = 60.0

        # POLY-SOT: track which trade IDs we've already alerted on so we
        # don't spam Telegram every time the SOT loop finds the same
        # divergence. Cleared on engine restart — that's intentional, an
        # operator-visible alert at engine startup is fine.
        #
        # POLY-SOT-b: dedupe key is now namespaced by table — e.g.
        # "manual_trades:42" vs "trades:42" — because manual_trades.id and
        # trades.id are independent integer sequences. Without the namespace
        # an alert on manual_trades #42 would suppress an alert on
        # automatic trades #42, hiding a real divergence.
        self._sot_alerted_trade_ids: set[str] = set()

        self._log = log.bind(component="clob_reconciler")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch poll_loop + report_loop as asyncio tasks."""
        self._log.info("reconciler.starting")
        await self._backfill_on_startup()
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="reconciler:poll"
        )
        self._report_task = asyncio.create_task(
            self._report_loop(), name="reconciler:report"
        )
        self._log.info("reconciler.started")

    async def stop(self) -> None:
        """Cancel background tasks."""
        self._log.info("reconciler.stopping")
        for task in (self._poll_task, self._report_task):
            if task and not task.done():
                task.cancel()
        tasks = [t for t in (self._poll_task, self._report_task) if t]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._log.info("reconciler.stopped")

    # ------------------------------------------------------------------
    # Backfill on startup
    # ------------------------------------------------------------------

    async def _backfill_on_startup(self) -> None:
        """Scan all on-chain positions, match to trades by token_id, update resolved ones."""
        try:
            outcomes = await self._poly.get_position_outcomes()
            if not outcomes:
                self._log.info("reconciler.backfill.no_positions")
                return

            backfilled = 0
            orphaned = 0

            for cid, data in outcomes.items():
                outcome = data["outcome"]

                if outcome == "OPEN":
                    continue

                # Mark as known so poll_loop doesn't re-alert
                self._known_resolved.add(cid)

                if not self._pool:
                    continue

                # Try to match and update unresolved trades by THIS position's
                # token_id. Previous implementation did `ORDER BY created_at
                # DESC LIMIT 20 / fetchrow` then updated by whatever token the
                # most-recent row happened to have — which meant the "match"
                # was blind and the UPDATE silently hit zero rows whenever
                # the latest unresolved trade wasn't the one that just
                # resolved. Two concurrent unresolved trades would guarantee
                # at least one mis-backfill.
                #
                # Fix: pull the position's own token_id from `data` (Polymarket
                # calls it "asset" but also sometimes "tokenId" — prefer asset,
                # fall back to tokenId). Use a prefix-match UPDATE because the
                # token_id length can vary between the CLOB wire format and
                # what the engine stored in metadata. `LIKE $1 || '%'` is a
                # left-anchored prefix match that plays well with the btree
                # index on metadata->>'token_id' if one exists.
                pos_token_id = str(data.get("asset", "") or data.get("tokenId", ""))
                if not pos_token_id:
                    # Polymarket returned a position without an asset/tokenId
                    # — can't match it to anything. Count as orphaned.
                    orphaned += 1
                    self._log.debug(
                        "reconciler.backfill.no_token_id",
                        condition_id=cid[:20],
                        outcome=outcome,
                    )
                    continue

                try:
                    async with self._pool.acquire() as conn:
                        # PnL: for WIN use Polymarket's computed pnl; for LOSS
                        # use negative cost (full stake lost). Both come from
                        # the position aggregate — not per-trade — but that's
                        # acceptable for backfill (we're resolving stale state,
                        # not making decisions).
                        pnl = data["pnl"] if outcome == "WIN" else -data["cost"]
                        status = (
                            "RESOLVED_WIN" if outcome == "WIN" else "RESOLVED_LOSS"
                        )
                        # Prefix-match in both directions to handle truncated
                        # or extended token IDs (the CLOB API sometimes
                        # returns padded values).
                        updated = await conn.execute(
                            """UPDATE trades SET outcome = $1, pnl_usd = $2,
                                      resolved_at = NOW(), status = $3
                               WHERE outcome IS NULL
                                 AND is_live = true
                                 AND metadata->>'token_id' IS NOT NULL
                                 AND (
                                     metadata->>'token_id' LIKE $4 || '%'
                                     OR $4 LIKE metadata->>'token_id' || '%'
                                 )""",
                            outcome,
                            pnl,
                            status,
                            pos_token_id,
                        )
                        # Parse "UPDATE N" suffix to get row count
                        row_count = 0
                        try:
                            row_count = int(str(updated).split()[-1])
                        except (ValueError, IndexError):
                            pass
                        if row_count > 0:
                            backfilled += row_count
                            # Tag the downstream trade_bible row(s) so the
                            # sitrep can filter startup-backfilled entries
                            # out of the "Recent wins/losses" display.
                            await conn.execute(
                                """UPDATE trade_bible
                                   SET resolution_source = 'backfill'
                                   WHERE trade_id IN (
                                       SELECT id FROM trades
                                       WHERE metadata->>'token_id' IS NOT NULL
                                         AND (
                                             metadata->>'token_id' LIKE $1 || '%'
                                             OR $1 LIKE metadata->>'token_id' || '%'
                                         )
                                         AND resolved_at > NOW() - INTERVAL '10 seconds'
                                   )""",
                                pos_token_id,
                            )
                            self._log.info(
                                "reconciler.backfill.updated",
                                condition_id=cid[:20],
                                token_id=pos_token_id[:20],
                                outcome=outcome,
                                rows=row_count,
                            )
                        else:
                            orphaned += 1
                            self._log.debug(
                                "reconciler.backfill.orphaned",
                                condition_id=cid[:20],
                                token_id=pos_token_id[:20],
                                outcome=outcome,
                            )
                except Exception as exc:
                    self._log.warning(
                        "reconciler.backfill.db_error",
                        condition_id=cid[:20],
                        error=str(exc)[:100],
                    )

            # Send startup report
            try:
                wallet = await self._poly.get_balance()
                open_count = sum(
                    1 for d in outcomes.values() if d["outcome"] == "OPEN"
                )
                open_value = sum(
                    d["value"] for d in outcomes.values() if d["outcome"] == "OPEN"
                )
                now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
                msg = (
                    f"*CLOB RECONCILER ONLINE* -- {now_str}\n"
                    f"----\n"
                    f"Wallet: `${wallet:.2f}` USDC\n"
                    f"Positions: `{len(outcomes)}` total\n"
                    f"  Open: `{open_count}` (`${open_value:.2f}` at risk)\n"
                    f"  Resolved: `{len(self._known_resolved)}`\n"
                    f"Backfilled: `{backfilled}` trades updated\n"
                    f"Orphaned: `{orphaned}` (no trade match)\n"
                    f"Poll interval: `{self._poll_interval}s`"
                )
                await self._alerter.send_raw_message(msg)
            except Exception as exc:
                self._log.warning(
                    "reconciler.backfill.report_failed", error=str(exc)[:100]
                )

            self._log.info(
                "reconciler.backfill.complete",
                total=len(outcomes),
                known_resolved=len(self._known_resolved),
                backfilled=backfilled,
                orphaned=orphaned,
            )
        except Exception as exc:
            self._log.error(
                "reconciler.backfill.failed", error=str(exc)[:200]
            )

    # ------------------------------------------------------------------
    # Poll loop (every 2s)
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Every 2s: fetch wallet + positions, detect new resolutions, update DB."""
        self._log.info("reconciler.poll_loop.started")

        while not self._shutdown.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.warning(
                    "reconciler.poll_error", error=str(exc)[:200]
                )

            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self._poll_interval
                )
                break  # shutdown signalled
            except asyncio.TimeoutError:
                pass  # normal timeout, continue polling

    async def _poll_once(self) -> None:
        """Single poll iteration."""
        now = datetime.now(timezone.utc)

        # 1. Wallet balance
        try:
            balance = await self._poly.get_balance()
            self._state.wallet = WalletSnapshot(
                balance_usdc=balance, fetched_at=now
            )

            # Persist wallet snapshot (sample: every 30th poll ~ 1/min)
            if self._pool and int(time.time()) % 60 < self._poll_interval:
                try:
                    async with self._pool.acquire() as conn:
                        await conn.execute(
                            """INSERT INTO wallet_snapshots (balance_usdc, source, recorded_at)
                               VALUES ($1, 'clob_reconciler', $2)""",
                            balance,
                            now,
                        )
                except Exception:
                    pass  # Non-fatal; table might not exist yet
        except Exception as exc:
            self._log.debug("reconciler.balance_error", error=str(exc)[:100])

        # 2. Positions
        try:
            outcomes = await self._poly.get_position_outcomes()
        except Exception as exc:
            self._log.debug("reconciler.positions_error", error=str(exc)[:100])
            return

        # Build position list for state
        positions: list[OpenPosition] = []
        for cid, data in outcomes.items():
            positions.append(
                OpenPosition(
                    condition_id=cid,
                    token_id=data.get("asset", "") or data.get("tokenId", ""),
                    size=data["size"],
                    avg_price=data["avgPrice"],
                    cost=data["cost"],
                    value=data["value"],
                    pnl=data["pnl"],
                    outcome=data["outcome"],
                )
            )
        self._state.positions = positions

        # 3. Detect new resolutions
        for cid, data in outcomes.items():
            if cid in self._known_resolved:
                continue

            outcome = data["outcome"]
            if outcome == "OPEN":
                continue

            # NEW resolution detected
            self._known_resolved.add(cid)
            await self._resolve_position(cid, data)

        # 4. Fetch resting orders (non-critical, errors swallowed)
        try:
            raw_orders = await self._poly.get_open_orders()
            resting: list[RestingOrder] = []
            for o in raw_orders:
                resting.append(
                    RestingOrder(
                        order_id=o.get("id", o.get("order_id", "")),
                        token_id=o.get("asset_id", o.get("token_id", "")),
                        price=float(o.get("price", 0)),
                        size_original=float(
                            o.get("original_size", o.get("size", 0))
                        ),
                        size_matched=float(o.get("size_matched", 0)),
                        status=o.get("status", "UNKNOWN"),
                    )
                )
            self._state.resting_orders = resting
        except Exception as exc:
            self._log.debug("reconciler.orders_error", error=str(exc)[:100])

        self._state.last_poll_at = now

        # 5. Orphan GTC fill check + trade_bible→trades sync (every ~60s)
        now_ts = time.time()
        if now_ts - self._last_orphan_check >= self._orphan_check_interval:
            self._last_orphan_check = now_ts
            try:
                await self._resolve_orphaned_fills()
            except Exception as exc:
                self._log.warning(
                    "reconciler.orphan_check_error", error=str(exc)[:200]
                )
            try:
                await self._sync_bible_to_trades()
            except Exception as exc:
                self._log.warning(
                    "reconciler.bible_sync_error", error=str(exc)[:200]
                )

    # ------------------------------------------------------------------
    # Orphan GTC fill resolution
    # ------------------------------------------------------------------

    async def _resolve_orphaned_fills(self) -> None:
        """Find trades marked EXPIRED/OPEN with confirmed CLOB fills, resolve them.

        Queries DB for trades that have clob_status=MATCHED but no outcome,
        then cross-references the CLOB trade history API to determine the
        oracle resolution (WIN/LOSS) and update the trade record.
        """
        if not self._pool:
            return

        # 1. Find orphaned trades in DB
        try:
            async with self._pool.acquire() as conn:
                orphans = await conn.fetch(
                    """SELECT id, order_id, direction,
                              metadata->>'token_id' as token_id,
                              metadata->>'clob_status' as clob_status,
                              metadata->>'shares_filled' as shares_filled,
                              metadata->>'entry_reason' as entry_reason,
                              stake_usd
                       FROM trades
                       WHERE status IN ('EXPIRED', 'OPEN', 'FILLED')
                         AND (metadata->>'clob_status' IN ('MATCHED', 'RESTING')
                              OR (COALESCE(NULLIF(metadata->>'shares_filled', ''), '0')::numeric > 0))
                         AND outcome IS NULL
                         AND is_live = true
                       ORDER BY created_at DESC
                       LIMIT 50""",
                )
        except Exception as exc:
            self._log.warning(
                "reconciler.orphan_query_error", error=str(exc)[:200]
            )
            return

        if not orphans:
            return

        self._log.info("reconciler.orphan_check", count=len(orphans))

        # 2. Fetch CLOB trade history (confirms fills) + position outcomes (resolution)
        try:
            fills = await self._poly.get_trade_history()
        except Exception as exc:
            self._log.warning(
                "reconciler.orphan_fills_error", error=str(exc)[:200]
            )
            fills = []

        try:
            positions = await self._poly.get_position_outcomes()
        except Exception as exc:
            self._log.warning(
                "reconciler.orphan_positions_error", error=str(exc)[:200]
            )
            positions = {}

        if not fills and not positions:
            self._log.debug("reconciler.orphan_no_data")
            return

        # Build lookup: asset_id -> fill data (most recent fill wins)
        fill_by_asset: dict[str, dict] = {}
        for f in fills:
            aid = f.get("asset_id", "")
            if aid:
                fill_by_asset[aid] = f

        # Build lookup: tokenId -> position data (includes resolution status)
        # Polymarket data API returns CLOB token ID in "asset" field, NOT "tokenId"
        pos_by_token: dict[str, dict] = {}
        for _cid, pdata in positions.items():
            tid = pdata.get("asset", "") or pdata.get("tokenId", "")
            if tid:
                pos_by_token[tid] = pdata

        # 3. Match orphans to fills/positions and resolve
        resolved_count = 0
        for orphan in orphans:
            token_id = orphan["token_id"]
            if not token_id:
                continue

            # Match by token_id — use startswith for length-mismatch tolerance
            matched_fill = None
            for aid, fill_data in fill_by_asset.items():
                if aid.startswith(token_id) or token_id.startswith(aid):
                    matched_fill = fill_data
                    break

            # Also check positions for this token_id
            matched_pos = None
            for tid, pdata in pos_by_token.items():
                if tid.startswith(token_id) or token_id.startswith(tid):
                    matched_pos = pdata
                    break

            if not matched_fill and not matched_pos:
                continue

            # Determine resolution from position data (source of truth)
            # Position curPrice >= 0.99 = WIN, <= 0.01 = LOSS
            pos_outcome = matched_pos.get("outcome", "OPEN") if matched_pos else None
            fill_price = float(matched_fill.get("price", 0)) if matched_fill else 0
            fill_size = float(matched_fill.get("size", 0)) if matched_fill else 0
            cost = float(orphan["stake_usd"] or 0)

            # ── display_shares: what we show the operator in telegram. ──
            # Previous implementation blindly took `orphan["shares_filled"]`
            # which sometimes held sentinel or mis-scaled values (999.00 was
            # observed in the wild on 2026-04-10 with a $0.45 fill and $1.01
            # cost, which is mathematically inconsistent). Prefer the
            # authoritative fill_size from the matched Polymarket fill if
            # it's positive; fall back to shares derived from cost/fill_price
            # (the true fill math: shares = stake_usd / fill_price); only
            # fall through to the DB column as a last resort.
            if fill_size > 0:
                display_shares = fill_size
            elif cost > 0 and fill_price > 0:
                display_shares = cost / fill_price
            elif orphan["shares_filled"]:
                db_shares = float(orphan["shares_filled"])
                # Reject sentinel/garbage values: if the DB says we hold
                # more shares than the whole position paid for at ≤$1 per
                # share, that's impossible and we should not display it.
                display_shares = db_shares if (cost <= 0 or db_shares <= cost / 0.01) else 0.0
            else:
                display_shares = 0.0

            if pos_outcome not in ("WIN", "LOSS"):
                # Market hasn't resolved yet — skip for now
                continue

            # Position outcome is already WIN/LOSS from get_position_outcomes()
            # WIN means curPrice >= 0.99 (token pays $1), LOSS means curPrice <= 0.01
            is_win = pos_outcome == "WIN"

            # ── Per-trade PnL: use the ACTUAL fill price, not a hardcoded cap. ──
            # Before the fix this used a hardcoded `trade_entry = 0.68` (the
            # V10 default cap), which produced e.g. +$0.48 for a fill that
            # should have returned +$1.09 (a $1.01 stake filled at $0.48
            # buys 2.10 shares, which pay $2.10 on WIN → PnL = +$1.09).
            # Now we derive effective_entry from the real fill when we have
            # it, fall back to the orphan's avg_price from the position,
            # and only use the legacy cap as a last resort with a warning.
            trade_stake = float(orphan["stake_usd"] or 0)
            avg_price_from_pos = (
                float(matched_pos.get("avgPrice", 0)) if matched_pos else 0.0
            )
            if fill_price > 0:
                effective_entry = fill_price
                entry_source = "fill"
            elif avg_price_from_pos > 0:
                effective_entry = avg_price_from_pos
                entry_source = "position_avg"
            else:
                effective_entry = 0.68  # legacy fallback, same as pre-fix behaviour
                entry_source = "legacy_cap"
                self._log.warning(
                    "reconciler.orphan_pnl_fallback",
                    trade_id=orphan["id"],
                    note="no fill_price or position avg_price available; using legacy $0.68 cap",
                )

            # Shares the trade actually holds: stake / entry. Only valid
            # when both are positive; otherwise we fall back to display_shares
            # and skip the PnL computation to avoid a divide-by-zero.
            if trade_stake > 0 and effective_entry > 0:
                trade_shares = trade_stake / effective_entry
            else:
                trade_shares = display_shares

            if is_win:
                outcome = "WIN"
                # Each held share pays $1 → payout = trade_shares × $1.
                # PnL = payout - stake = trade_shares - trade_stake.
                if trade_stake > 0 and trade_shares > 0:
                    pnl = round(trade_shares - trade_stake, 2)
                else:
                    pnl = round(display_shares - cost, 2)
                status = "RESOLVED_WIN"
            else:
                outcome = "LOSS"
                pnl = round(-trade_stake, 2) if trade_stake > 0 else round(-cost, 2)
                status = "RESOLVED_LOSS"

            # Sanity: if WIN but PnL is negative, something is wrong — skip
            if is_win and pnl < 0:
                self._log.warning("reconciler.orphan_pnl_mismatch",
                    trade_id=orphan["id"], outcome="WIN", pnl=f"${pnl:.2f}",
                    display_shares=display_shares, cost=cost,
                    trade_stake=trade_stake, effective_entry=effective_entry,
                    entry_source=entry_source)
                continue

            # 4. Update DB
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE trades
                           SET outcome = $1, pnl_usd = $2,
                               resolved_at = NOW(), status = $3
                           WHERE id = $4 AND outcome IS NULL""",
                        outcome,
                        pnl,
                        status,
                        orphan["id"],
                    )
                    # Tag the downstream trade_bible row with
                    # `resolution_source='orphan_resolved'` so the sitrep
                    # can distinguish orphan-reconciler resolutions from
                    # live-engine trigger resolutions. Previously these
                    # landed with resolution_source=NULL and the sitrep
                    # showed them indistinguishably from fresh fills.
                    await conn.execute(
                        """UPDATE trade_bible
                           SET resolution_source = 'orphan_resolved'
                           WHERE trade_id = $1""",
                        orphan["id"],
                    )
                resolved_count += 1

                self._log.info(
                    "reconciler.orphan_resolved",
                    trade_id=orphan["id"],
                    token_id=token_id[:20],
                    outcome=outcome,
                    pnl=f"${pnl:.2f}",
                )

                # Track for 5-min report
                if is_win:
                    self._report_wins.append(pnl)
                else:
                    self._report_losses.append(cost)

            except Exception as exc:
                self._log.warning(
                    "reconciler.orphan_update_error",
                    trade_id=orphan["id"],
                    error=str(exc)[:100],
                )
                continue

            # 5. Send Telegram notification
            try:
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                # PnL display: show the actual computed pnl for losses too
                # (previously hardcoded to -cost, which ignored trade_stake
                # and showed the wrong magnitude for partial fills).
                pnl_str = f"+${pnl:.2f}" if is_win else f"${pnl:.2f}"
                emoji = "WIN" if is_win else "LOSS"
                reason_line = ""
                if orphan["entry_reason"]:
                    reason_line = f"Entry: `{orphan['entry_reason']}`\n"

                trade_direction = (orphan["direction"] or "YES").upper()
                # Entry-price display: show the fill price if we have one,
                # otherwise mark the fallback source so the operator can
                # tell at a glance if the PnL used a synthetic entry.
                # This is the visual fix for the "999.00 @ $0.4500 cost $1.01"
                # sitrep glitch that shipped in the old code — the shares
                # and entry-price values weren't from the same source.
                entry_display = (
                    f"${effective_entry:.4f}"
                    if entry_source != "legacy_cap"
                    else f"${effective_entry:.4f} (fallback)"
                )
                msg = (
                    f"*{emoji} -- ORPHAN RESOLVED* (GTC fill)\n"
                    f"`{now_str}`\n"
                    f"\n"
                    f"Direction: `{trade_direction}`\n"
                    f"Resolution: `{pos_outcome}`\n"
                    f"Shares: `{display_shares:.2f}` @ `{entry_display}`\n"
                    f"Cost: `${cost:.2f}`\n"
                    f"P&L: `{pnl_str}`\n"
                    f"{reason_line}"
                    f"\n"
                    f"_Source: CLOB Orphan Reconciler_"
                )
                await self._alerter.send_raw_message(msg)
            except Exception as exc:
                self._log.debug(
                    "reconciler.orphan_notify_failed", error=str(exc)[:100]
                )

        if resolved_count > 0:
            self._log.info(
                "reconciler.orphan_check_complete",
                resolved=resolved_count,
                checked=len(orphans),
            )

    # ------------------------------------------------------------------
    # Resolution handler
    # ------------------------------------------------------------------

    async def _resolve_position(self, condition_id: str, data: dict) -> None:
        """Match to trades by token_id, UPDATE outcome/pnl/resolved_at/status."""
        outcome = data["outcome"]
        size = data["size"]
        avg_price = data["avgPrice"]
        cost = data["cost"]
        value = data["value"]
        pnl_raw = data["pnl"]

        pnl = pnl_raw if outcome == "WIN" else -cost
        status = "RESOLVED_WIN" if outcome == "WIN" else "RESOLVED_LOSS"

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        # Track for 5-min report
        if outcome == "WIN":
            self._report_wins.append(pnl)
        else:
            self._report_losses.append(cost)

        # Match to trades in DB by token_id
        matched_trade_id = None
        matched_reason = None
        matched_token_id = None

        # Extract token_id from position data for exact matching
        # Polymarket data API returns CLOB token ID in "asset" field, NOT "tokenId"
        _pos_token_id = str(data.get("asset", "") or data.get("tokenId", ""))

        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    # Primary: EXACT match by token_id from Polymarket position
                    match = None
                    if _pos_token_id:
                        match = await conn.fetchrow(
                            """SELECT id, metadata->>'entry_reason' as reason,
                                      metadata->>'token_id' as token_id
                               FROM trades
                               WHERE metadata->>'token_id' = $1
                                 AND is_live = true
                                 AND outcome IS NULL
                               ORDER BY created_at DESC LIMIT 1""",
                            _pos_token_id,
                        )

                    # Prefix match fallback: token_id may differ by 1 char in length.
                    # PE-02 fix: using two parameters ($1 and $2) in the
                    # bidirectional LIKE made asyncpg unable to deduce whether
                    # the placeholder was text or varchar — it would fail with
                    # "inconsistent types deduced for parameter $1 — text vs
                    # character varying". Cast a single parameter to ::text
                    # explicitly, matching the working pattern in the startup
                    # backfill (lines 185-186).
                    if not match and _pos_token_id and len(_pos_token_id) > 10:
                        match = await conn.fetchrow(
                            """SELECT id, metadata->>'entry_reason' as reason,
                                      metadata->>'token_id' as token_id
                               FROM trades
                               WHERE is_live = true
                                 AND outcome IS NULL
                                 AND metadata->>'token_id' IS NOT NULL
                                 AND (metadata->>'token_id' LIKE $1::text || '%'
                                      OR $1::text LIKE metadata->>'token_id' || '%')
                               ORDER BY created_at DESC LIMIT 1""",
                            _pos_token_id,
                        )
                        if match:
                            self._log.info(
                                "reconciler.prefix_match",
                                pos_token=_pos_token_id[:20],
                                db_token=(match["token_id"] or "")[:20],
                            )

                    # Fallback: match by approximate cost + recency when token matching fails
                    if not match and cost > 0:
                        match = await conn.fetchrow(
                            """SELECT id, metadata->>'entry_reason' as reason,
                                      metadata->>'token_id' as token_id
                               FROM trades
                               WHERE is_live = true
                                 AND outcome IS NULL
                                 AND ABS(stake_usd - $1) < 0.50
                               ORDER BY created_at DESC LIMIT 1""",
                            cost,
                        )
                        if match:
                            self._log.info(
                                "reconciler.cost_fallback_match",
                                trade_id=match["id"],
                                cost=f"${cost:.2f}",
                                condition_id=condition_id[:20],
                            )

                    if match:
                        matched_trade_id = match["id"]
                        matched_reason = match["reason"]
                        matched_token_id = match["token_id"]

                        # Use per-trade stake from DB, not Polymarket aggregate cost
                        trade_row = await conn.fetchrow(
                            "SELECT stake_usd, entry_price FROM trades WHERE id = $1",
                            matched_trade_id,
                        )
                        trade_stake = float(trade_row["stake_usd"]) if trade_row and trade_row["stake_usd"] else cost
                        trade_entry = float(trade_row["entry_price"]) if trade_row and trade_row["entry_price"] else avg_price
                        trade_shares = trade_stake / trade_entry if trade_entry > 0 else size

                        # PnL from per-trade data: WIN = shares - stake, LOSS = -stake
                        if outcome == "WIN":
                            trade_pnl = round(trade_shares - trade_stake, 4)
                        else:
                            trade_pnl = round(-trade_stake, 4)

                        # PE-05 fix: the previous UPDATE used `$1` in both an
                        # assignment (`SET outcome = $1`) and a comparison
                        # inside a CASE WHEN (`CASE WHEN $1 = 'WIN'`). asyncpg
                        # couldn't reconcile those two type contexts when
                        # `outcome` was declared varchar but the literal
                        # `'WIN'` deduced as text — raising "inconsistent
                        # types deduced for parameter $1 — text versus
                        # character varying". The status value is already
                        # pre-computed at line 720, so use it directly as
                        # a separate parameter and drop the CASE WHEN.
                        await conn.execute(
                            """UPDATE trades SET outcome = $1, pnl_usd = $2,
                                      resolved_at = NOW(), status = $3
                               WHERE id = $4 AND outcome IS NULL""",
                            outcome,
                            trade_pnl,
                            status,
                            matched_trade_id,
                        )
                        self._log.info(
                            "reconciler.trade_resolved",
                            trade_id=matched_trade_id,
                            token_id=matched_token_id[:20] if matched_token_id else "?",
                            outcome=outcome,
                            pnl=f"${trade_pnl:.2f}",
                            poly_cost=f"${cost:.2f}",
                        )
                    else:
                        # Log raw position data for debugging match failures
                        _raw_keys = list(data.keys())
                        self._log.warning(
                            "reconciler.no_trade_match",
                            condition_id=condition_id[:20],
                            pos_token=_pos_token_id[:20] if _pos_token_id else "?",
                            raw_asset=str(data.get("asset", ""))[:30],
                            raw_tokenId=str(data.get("tokenId", ""))[:30],
                            raw_keys=str(_raw_keys)[:100],
                            cost=f"${cost:.2f}",
                            outcome=outcome,
                            size=size,
                            avg_price=avg_price,
                        )
            except Exception as exc:
                self._log.warning(
                    "reconciler.resolve_db_error",
                    condition_id=condition_id[:20],
                    error=str(exc)[:100],
                )

        # Send Telegram notification (use per-trade data when matched, Polymarket aggregate as fallback)
        try:
            if matched_trade_id:
                _notify_pnl = trade_pnl
                _notify_shares = trade_shares
                _notify_price = trade_entry
                _notify_cost = trade_stake
                _source = "CLOB Reconciler"
            else:
                _notify_pnl = pnl
                _notify_shares = size
                _notify_price = avg_price
                _notify_cost = cost
                _source = "CLOB Reconciler (aggregate)"

            if outcome == "WIN":
                emoji = "WIN"
                pnl_str = f"+${_notify_pnl:.2f}"
            else:
                emoji = "LOSS"
                pnl_str = f"-${_notify_cost:.2f}"

            reason_line = ""
            if matched_reason:
                reason_line = f"Entry: `{matched_reason}`\n"

            wallet_str = ""
            if self._state.wallet:
                wallet_str = f"\nWallet: `${self._state.wallet.balance_usdc:.2f}` USDC"

            msg = (
                f"*{emoji} -- BTC* (LIVE)\n"
                f"`{now_str}`\n"
                f"\n"
                f"*Result*\n"
                f"Shares: `{_notify_shares:.2f}`\n"
                f"Avg Price: `${_notify_price:.4f}`\n"
                f"Cost: `${_notify_cost:.2f}`\n"
                f"P&L: `{pnl_str}`\n"
                f"{reason_line}"
                f"{wallet_str}\n"
                f"\n"
                f"_Source: {_source}_"
            )
            await self._alerter.send_raw_message(msg)
        except Exception as exc:
            self._log.debug(
                "reconciler.notification_failed", error=str(exc)[:100]
            )

    # ------------------------------------------------------------------
    # trade_bible → trades table sync
    # ------------------------------------------------------------------

    async def _sync_bible_to_trades(self) -> None:
        """Backfill trades table from trade_bible for any resolved trades
        where trade_bible has outcome but trades table doesn't.

        This ensures the SITREP and all queries against the trades table
        show accurate win/loss data, even for orphans resolved by the reconciler.
        """
        if not self._pool:
            return

        try:
            async with self._pool.acquire() as conn:
                # Find mismatches: trade_bible has outcome, trades doesn't
                mismatches = await conn.fetch(
                    """SELECT tb.trade_id, tb.trade_outcome, tb.pnl_usd, tb.resolved_at
                       FROM trade_bible tb
                       JOIN trades t ON t.id = tb.trade_id
                       WHERE tb.trade_outcome IS NOT NULL
                         AND t.outcome IS NULL
                         AND tb.is_live = true
                       LIMIT 20"""
                )

                if not mismatches:
                    return

                synced = 0
                for m in mismatches:
                    status = "RESOLVED_WIN" if m["trade_outcome"] == "WIN" else "RESOLVED_LOSS"
                    await conn.execute(
                        """UPDATE trades
                           SET outcome = $1, pnl_usd = $2, resolved_at = $3, status = $4
                           WHERE id = $5 AND outcome IS NULL""",
                        m["trade_outcome"],
                        m["pnl_usd"],
                        m["resolved_at"],
                        status,
                        m["trade_id"],
                    )
                    synced += 1

                if synced > 0:
                    self._log.info(
                        "reconciler.bible_sync", synced=synced,
                    )
        except Exception as exc:
            self._log.warning(
                "reconciler.bible_sync_error", error=str(exc)[:200]
            )

    # ------------------------------------------------------------------
    # Report loop (every 5 min)
    # ------------------------------------------------------------------

    async def _report_loop(self) -> None:
        """Every 5 min: send Telegram reconciliation report."""
        self._log.info("reconciler.report_loop.started")

        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self._report_interval
                )
                break  # shutdown signalled
            except asyncio.TimeoutError:
                pass  # normal timeout, send report

            try:
                await self._send_report()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.warning(
                    "reconciler.report_error", error=str(exc)[:200]
                )

    async def _send_report(self) -> None:
        """Compose and send the 5-minute reconciliation report."""
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%H:%M UTC")

        # Wallet line
        wallet_line = "Wallet: _unavailable_"
        if self._state.wallet:
            wallet_line = f"Wallet: `${self._state.wallet.balance_usdc:.2f}` USDC (CLOB verified)"

        # Last 5 min activity
        activity_lines = []
        if self._report_wins:
            win_details = ", ".join(f"+${w:.2f}" for w in self._report_wins)
            activity_lines.append(f"  {len(self._report_wins)}W: {win_details}")
        if self._report_losses:
            loss_details = ", ".join(f"-${l:.2f}" for l in self._report_losses)
            activity_lines.append(f"  {len(self._report_losses)}L: {loss_details}")
        if self._report_filled > 0:
            activity_lines.append(f"  {self._report_filled} orders filled")
        if self._report_expired > 0:
            activity_lines.append(f"  {self._report_expired} orders expired")

        if not activity_lines:
            activity_lines.append("  No activity")

        activity_block = "\n".join(activity_lines)

        # Open positions
        open_positions = [
            p for p in self._state.positions if p.outcome == "OPEN"
        ]
        open_value = sum(p.cost for p in open_positions)
        open_line = f"Open positions: `{len(open_positions)}` (`${open_value:.2f}` at risk)"

        # Resting orders
        resting_count = len(self._state.resting_orders)
        resting_line = f"Resting GTC: `{resting_count}` orders on book"

        msg = (
            f"*CLOB RECONCILIATION* -- {now_str}\n"
            f"----\n"
            f"{wallet_line}\n"
            f"\n"
            f"*Last 5 min:*\n"
            f"{activity_block}\n"
            f"\n"
            f"{open_line}\n"
            f"{resting_line}"
        )

        await self._alerter.send_raw_message(msg)
        self._state.last_report_at = now

        # Reset report counters
        self._report_wins.clear()
        self._report_losses.clear()
        self._report_filled = 0
        self._report_expired = 0

        self._log.info(
            "reconciler.report_sent",
            open=len(open_positions),
            resting=resting_count,
        )

    # ------------------------------------------------------------------
    # POLY-SOT — Polymarket CLOB source-of-truth for manual trades
    # ------------------------------------------------------------------
    #
    # Mirrors the margin_engine pattern where the exchange API is the
    # authoritative record for every position. For Polymarket manual trades:
    #
    #   1. The user clicks "Execute" on the live trade panel.
    #   2. The hub writes a row into manual_trades with status='pending_live'.
    #   3. The orchestrator's manual_trade_poller picks it up and calls
    #      poly_client.place_order(...) which returns a CLOB order ID. The
    #      poller persists that ID into the new polymarket_order_id column
    #      and flips status to 'open' / 'executed'.
    #   4. THIS METHOD then re-queries Polymarket and stamps the row with
    #      the authoritative polymarket_confirmed_* fields plus a
    #      sot_reconciliation_state describing whether the engine and
    #      Polymarket agree.
    #
    # Without this loop, if the place_order() call somehow times out, retries,
    # or partially executes, the engine DB would happily claim success while
    # Polymarket never actually booked the trade. This is exactly the failure
    # mode the user flagged on 2026-04-11.

    def _compare_to_polymarket(
        self,
        engine_row: dict,
        poly_status: "Optional[PolyOrderStatus]",
    ) -> dict:
        """Pure comparison: engine row + Polymarket status → SOT decision.

        POLY-SOT-b: extracted from the body of ``reconcile_manual_trades_sot``
        so the new ``reconcile_trades_sot`` (and any future per-row caller)
        can re-use the exact same decision matrix without copy-paste drift.

        The caller is responsible for:
          * fetching the engine row
          * calling ``poly_client.get_order_status_sot(...)`` (or passing None
            if there is no order ID / Polymarket returned no record)
          * persisting the result via the appropriate ``update_*_sot`` helper
          * firing the Telegram alert (if ``should_alert=True``)
          * incrementing the right counter on the summary

        Returns a dict with keys:
          * ``state``: one of agrees | unreconciled | engine_optimistic |
            polymarket_only | diverged
          * ``notes``: human-readable explanation (or None on agrees)
          * ``confirmed_status``, ``confirmed_price``, ``confirmed_size``,
            ``confirmed_at``: values to write back to the row (may be None)
          * ``should_alert``: True iff the state warrants a Telegram alert
        """
        engine_status = (engine_row.get("status") or "").lower()
        engine_fill_price = _to_float(engine_row.get("entry_price"))

        # Case 1: Polymarket has no record at all (None means 404 or empty).
        if poly_status is None:
            if engine_status in ("failed_no_token",) or engine_status.startswith(
                "failed"
            ):
                # Engine knows it failed AND Polymarket also has nothing —
                # they agree on the negative. Mark agrees, no alert.
                return {
                    "state": "agrees",
                    "notes": "engine failed and polymarket has no record — both agree on no fill",
                    "confirmed_status": None,
                    "confirmed_price": None,
                    "confirmed_size": None,
                    "confirmed_at": None,
                    "should_alert": False,
                }
            poly_order_id = engine_row.get("polymarket_order_id")
            notes = (
                f"engine status={engine_status} order_id={str(poly_order_id)[:20]} "
                f"— polymarket returned no record (404 or empty)"
            )
            return {
                "state": "engine_optimistic",
                "notes": notes,
                "confirmed_status": None,
                "confirmed_price": None,
                "confirmed_size": None,
                "confirmed_at": None,
                "should_alert": True,
            }

        # Polymarket returned a record — extract its fields.
        confirmed_status = poly_status.status
        confirmed_price = poly_status.fill_price
        confirmed_size = poly_status.fill_size
        confirmed_at = poly_status.timestamp

        # Case 2: engine says failed but Polymarket has a fill — polymarket_only.
        if engine_status.startswith("failed") and poly_status.is_filled:
            notes = (
                f"engine status={engine_status} but polymarket has filled order "
                f"size={confirmed_size} price={confirmed_price}"
            )
            return {
                "state": "polymarket_only",
                "notes": notes,
                "confirmed_status": confirmed_status,
                "confirmed_price": confirmed_price,
                "confirmed_size": confirmed_size,
                "confirmed_at": confirmed_at,
                "should_alert": True,
            }

        # Case 3: Polymarket order not yet terminal — unreconciled, no alert.
        if not poly_status.is_terminal:
            return {
                "state": "unreconciled",
                "notes": f"polymarket status={confirmed_status} (still pending)",
                "confirmed_status": confirmed_status,
                "confirmed_price": confirmed_price,
                "confirmed_size": confirmed_size,
                "confirmed_at": confirmed_at,
                "should_alert": False,
            }

        # Case 4: Terminal — compare numbers.
        divergence_notes: list[str] = []
        if (
            engine_fill_price is not None
            and confirmed_price is not None
            and engine_fill_price > 0
        ):
            price_pct = (
                abs(confirmed_price - engine_fill_price) / engine_fill_price * 100.0
            )
            if price_pct > self._sot_price_tolerance_pct:
                divergence_notes.append(
                    f"price diff {price_pct:.2f}% (engine={engine_fill_price:.4f} "
                    f"poly={confirmed_price:.4f})"
                )

        if (
            engine_status in ("executed", "open", "live", "filled", "matched")
            and confirmed_size is not None
            and confirmed_size <= 0
        ):
            divergence_notes.append("polymarket fill_size = 0 despite terminal status")

        if divergence_notes:
            return {
                "state": "diverged",
                "notes": "; ".join(divergence_notes),
                "confirmed_status": confirmed_status,
                "confirmed_price": confirmed_price,
                "confirmed_size": confirmed_size,
                "confirmed_at": confirmed_at,
                "should_alert": True,
            }

        # Case 5: All checks passed → agrees.
        return {
            "state": "agrees",
            "notes": None,
            "confirmed_status": confirmed_status,
            "confirmed_price": confirmed_price,
            "confirmed_size": confirmed_size,
            "confirmed_at": confirmed_at,
            "should_alert": False,
        }

    # ──────────────────────────────────────────────────────────────────────
    # POLY-SOT-d — on-chain-based comparison against `poly_fills`.
    #
    # The CLOB `/get_order/{clob_order_id}` endpoint (used by the legacy
    # ``_compare_to_polymarket`` above) has a short retention window: trades
    # older than a few days return empty, which produces false-positive
    # `engine_optimistic` tags. The `poly_fills` table is populated by a
    # separate worker (``poly_fills_reconciler``) from the
    # data-api.polymarket.com endpoint, which has no retention window and
    # returns every on-chain fill for our proxy wallet. That makes it the
    # definitive SOT record.
    #
    # The new helper compares an engine trade row to a matched poly_fills
    # row (or the lack thereof). It is intentionally structured so the
    # forward reconciler and the one-shot backfill use the same decision
    # matrix — mirroring the existing ``_compare_to_polymarket`` pattern.
    # ──────────────────────────────────────────────────────────────────────

    def _compare_to_polymarket_onchain(
        self,
        trade: dict,
        fill: Optional[dict],
    ) -> dict:
        """Compare an engine trade row to a matched poly_fills row.

        Returns a dict with keys:
          * ``state`` — one of agrees | unreconciled | engine_optimistic |
            polymarket_only | diverged | paper
          * ``notes`` — human-readable explanation
          * ``confirmed_status`` — synthetic 'matched' when we have a fill,
            None otherwise (poly_fills has no pending concept)
          * ``confirmed_price`` / ``confirmed_size`` / ``confirmed_at`` —
            values to stamp on the trade row
          * ``tx_hash`` — the on-chain Polygon tx hash (None when no fill)
          * ``should_alert`` — True iff the state warrants a Telegram alert

        Decision matrix
        ---------------
          1. Paper trade (order_id ~ 5min-/manual-paper- or clob_order_id
             NULL and not a 0x hash) → ``paper`` terminal state, no alert,
             no need to ever check poly_fills (paper never touched the CLOB).
          2. Live trade + fill is None + age <= 10min → ``unreconciled``
             (poly_fills worker may still catch up on the next pass).
          3. Live trade + fill is None + age >  10min → ``engine_optimistic``
             (the row should have a poly_fills entry by now — this is the
             real engine-vs-reality divergence set).
          4. Live trade + fill present → compare price diff bps +
             size diff pct against configured tolerances. Inside → ``agrees``
             (notes include the tx hash as proof). Outside → ``diverged``
             (notes describe the drift).

        The ``polymarket_only`` state is preserved for completeness but is
        very rare under the poly_fills path — it would only fire when the
        operator placed a trade directly from their wallet, bypassing the
        engine. Upstream code (``reconcile_trades_sot``) handles that case
        with a separate query against unmatched poly_fills rows.
        """
        # Path 1: paper trade — always terminal, never re-checked.
        if _is_paper_trade(trade):
            return {
                "state": "paper",
                "notes": "paper trade — never touched Polymarket CLOB",
                "confirmed_status": None,
                "confirmed_price": None,
                "confirmed_size": None,
                "confirmed_at": None,
                "tx_hash": None,
                "should_alert": False,
            }

        # Path 2: no matching poly_fills row.
        if fill is None:
            age_minutes = _row_age_minutes(trade)
            if age_minutes is not None and age_minutes <= 10.0:
                # Still young — give the poly_fills worker more time.
                return {
                    "state": "unreconciled",
                    "notes": (
                        f"no poly_fills match yet (age {age_minutes:.1f}min)"
                    ),
                    "confirmed_status": None,
                    "confirmed_price": None,
                    "confirmed_size": None,
                    "confirmed_at": None,
                    "tx_hash": None,
                    "should_alert": False,
                }
            # Older than 10 minutes with no on-chain fill — real divergence.
            age_str = (
                f"{age_minutes:.1f}min" if age_minutes is not None else "unknown age"
            )
            return {
                "state": "engine_optimistic",
                "notes": (
                    f"live trade with no matching poly_fills row "
                    f"(age {age_str}) — engine claimed FILLED but the "
                    f"on-chain data-api has no record"
                ),
                "confirmed_status": None,
                "confirmed_price": None,
                "confirmed_size": None,
                "confirmed_at": None,
                "tx_hash": None,
                "should_alert": True,
            }

        # Path 3: fill is present. Compare price + size.
        poly_price = _to_float(fill.get("poly_price"))
        poly_size = _to_float(fill.get("poly_size"))
        poly_match_time = fill.get("match_time_utc")
        tx_hash = fill.get("transaction_hash")

        engine_price = _to_float(
            trade.get("fill_price") or trade.get("entry_price")
        )

        divergence_notes: list[str] = []
        if (
            engine_price is not None
            and poly_price is not None
            and engine_price > 0
        ):
            price_pct = abs(poly_price - engine_price) / engine_price * 100.0
            if price_pct > self._sot_price_tolerance_pct:
                divergence_notes.append(
                    f"price diff {price_pct:.2f}% "
                    f"(engine={engine_price:.4f} poly={poly_price:.4f})"
                )

        # Size: engine may not have a size populated on historical rows. If
        # both are populated and differ by more than 1 share (0 tolerance
        # was too strict — integer rounding on the CLOB means a 7.27-share
        # engine fill can land as 7.265 on-chain) we flag it. Use 1% as a
        # conservative band.
        engine_size = _to_float(trade.get("fill_size"))
        if (
            engine_size is not None
            and poly_size is not None
            and engine_size > 0
        ):
            size_pct = abs(poly_size - engine_size) / engine_size * 100.0
            if size_pct > 1.0:
                divergence_notes.append(
                    f"size diff {size_pct:.2f}% "
                    f"(engine={engine_size:.3f} poly={poly_size:.3f})"
                )

        if divergence_notes:
            return {
                "state": "diverged",
                "notes": "; ".join(divergence_notes) + (
                    f" tx={str(tx_hash)[:18]}..." if tx_hash else ""
                ),
                "confirmed_status": "matched",
                "confirmed_price": poly_price,
                "confirmed_size": poly_size,
                "confirmed_at": poly_match_time,
                "tx_hash": tx_hash,
                "should_alert": True,
            }

        # All checks passed → agrees with on-chain proof.
        tx_display = f"tx={str(tx_hash)[:18]}..." if tx_hash else "tx=unknown"
        return {
            "state": "agrees",
            "notes": f"on-chain fill confirmed: {tx_display}",
            "confirmed_status": "matched",
            "confirmed_price": poly_price,
            "confirmed_size": poly_size,
            "confirmed_at": poly_match_time,
            "tx_hash": tx_hash,
            "should_alert": False,
        }

    # ──────────────────────────────────────────────────────────────────────
    # POLY-SOT-d — reconciler rewrite to use `poly_fills` as the SOT.
    #
    # Both ``reconcile_manual_trades_sot`` and ``reconcile_trades_sot`` now
    # run a SQL LEFT JOIN against the `poly_fills` table instead of making
    # N separate calls to the CLOB `/get_order` API. This is:
    #
    #   * cheaper (one query vs N HTTP round trips per pass)
    #   * correct on historical data (poly_fills has no retention window;
    #     the CLOB API returns empty for trades older than a few days)
    #   * faster to converge on paper trades (they're tagged `paper`
    #     immediately instead of going through the CLOB 404 path)
    #
    # The CLOB API client (`get_order_status_sot`) is preserved in
    # ``execution/polymarket_client.py`` — it's still useful for the forward
    # path on very fresh manual trades — but the reconciler loop no longer
    # calls it. Tests that want to exercise the old path can call
    # ``_compare_to_polymarket`` directly; new tests should call
    # ``_compare_to_polymarket_onchain``.
    # ──────────────────────────────────────────────────────────────────────

    async def reconcile_manual_trades_sot(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> "ReconciliationSummary":
        """Source-of-truth reconciliation for manual_trades rows.

        Walks every manual_trades row whose ``sot_reconciliation_state`` is
        stale (NULL / unreconciled / a non-terminal divergence) and joins
        it against ``poly_fills`` to decide the new state. See the
        ``_compare_to_polymarket_onchain`` docstring for the decision
        matrix.

        Returns a ``ReconciliationSummary`` with per-state counts. Per-row
        state (and the on-chain tx hash when matched) is persisted via
        ``_PoolDBClient.update_manual_trade_sot``.
        """
        summary = ReconciliationSummary()
        if not self._pool:
            return summary

        db = _PoolDBClient(self._pool)

        joined_rows = await db.fetch_manual_trades_joined_poly_fills(
            since=since, limit=limit
        )
        summary.checked = len(joined_rows)
        if not joined_rows:
            return summary

        for trade_row, fill_row in joined_rows:
            trade_id = trade_row["trade_id"]
            engine_status = (trade_row.get("status") or "").lower()

            row_record: dict = {
                "trade_id": trade_id,
                "engine_status": engine_status,
                "table": "manual_trades",
            }

            try:
                decision = self._compare_to_polymarket_onchain(trade_row, fill_row)
            except Exception as exc:
                summary.errors += 1
                self._log.warning(
                    "reconcile_sot.compare_failed",
                    trade_id=trade_id,
                    error=str(exc)[:200],
                )
                row_record["state"] = "error"
                summary.rows.append(row_record)
                continue

            state = decision["state"]

            await db.update_manual_trade_sot(
                trade_id=trade_id,
                polymarket_confirmed_status=decision["confirmed_status"],
                polymarket_confirmed_fill_price=decision["confirmed_price"],
                polymarket_confirmed_size=decision["confirmed_size"],
                polymarket_confirmed_at=decision["confirmed_at"],
                sot_reconciliation_state=state,
                sot_reconciliation_notes=decision["notes"],
                polymarket_tx_hash=decision.get("tx_hash"),
            )

            if state == "agrees":
                summary.agrees += 1
                # Clear any prior alert dedupe so a future regression alerts.
                self._sot_alerted_trade_ids.discard(f"manual_trades:{trade_id}")
                self._sot_alerted_trade_ids.discard(trade_id)  # legacy fallback
            elif state == "unreconciled":
                summary.unreconciled += 1
            elif state == "engine_optimistic":
                summary.engine_optimistic += 1
            elif state == "polymarket_only":
                summary.polymarket_only += 1
            elif state == "diverged":
                summary.diverged += 1
            elif state == "paper":
                summary.paper += 1

            if decision.get("should_alert"):
                if await self._fire_sot_alert(
                    trade_id, state, decision.get("notes") or "", trade_row,
                    table="manual_trades",
                ):
                    summary.alerts_fired += 1

            row_record["state"] = state
            summary.rows.append(row_record)

        self._log.info(
            "reconcile_sot.complete",
            checked=summary.checked,
            agrees=summary.agrees,
            unreconciled=summary.unreconciled,
            engine_optimistic=summary.engine_optimistic,
            polymarket_only=summary.polymarket_only,
            diverged=summary.diverged,
            paper=summary.paper,
            errors=summary.errors,
            alerts=summary.alerts_fired,
        )
        return summary

    # ------------------------------------------------------------------
    # POLY-SOT-d — same SOT pass against the `trades` table for automatic
    # engine-initiated trades. Shares the ``_compare_to_polymarket_onchain``
    # helper above so the decision matrix stays in lock-step with the
    # manual_trades pass.
    # ------------------------------------------------------------------

    async def reconcile_trades_sot(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> "ReconciliationSummary":
        """Source-of-truth reconciliation for the `trades` (automatic) table.

        Mirrors ``reconcile_manual_trades_sot`` but walks the
        automatic-trade ``trades`` table. The two methods share the
        ``_compare_to_polymarket_onchain`` helper so the decision matrix
        stays consistent; the only differences are the table being queried
        and the alert dedupe namespace.
        """
        summary = ReconciliationSummary()
        if not self._pool:
            return summary

        db = _TradesPoolDBClient(self._pool)

        joined_rows = await db.fetch_trades_joined_poly_fills(
            since=since, limit=limit
        )
        summary.checked = len(joined_rows)
        if not joined_rows:
            return summary

        for trade_row, fill_row in joined_rows:
            trade_id = trade_row["trade_id"]
            engine_status = (trade_row.get("status") or "").lower()

            row_record: dict = {
                "trade_id": trade_id,
                "engine_status": engine_status,
                "table": "trades",
            }

            try:
                decision = self._compare_to_polymarket_onchain(trade_row, fill_row)
            except Exception as exc:
                summary.errors += 1
                self._log.warning(
                    "reconcile_trades_sot.compare_failed",
                    trade_id=trade_id,
                    error=str(exc)[:200],
                )
                row_record["state"] = "error"
                summary.rows.append(row_record)
                continue

            state = decision["state"]

            await db.update_trade_sot(
                trade_id=trade_id,
                polymarket_confirmed_status=decision["confirmed_status"],
                polymarket_confirmed_fill_price=decision["confirmed_price"],
                polymarket_confirmed_size=decision["confirmed_size"],
                polymarket_confirmed_at=decision["confirmed_at"],
                sot_reconciliation_state=state,
                sot_reconciliation_notes=decision["notes"],
                polymarket_tx_hash=decision.get("tx_hash"),
            )

            if state == "agrees":
                summary.agrees += 1
                self._sot_alerted_trade_ids.discard(f"trades:{trade_id}")
            elif state == "unreconciled":
                summary.unreconciled += 1
            elif state == "engine_optimistic":
                summary.engine_optimistic += 1
            elif state == "polymarket_only":
                summary.polymarket_only += 1
            elif state == "diverged":
                summary.diverged += 1
            elif state == "paper":
                summary.paper += 1

            if decision.get("should_alert"):
                if await self._fire_sot_alert(
                    trade_id, state, decision.get("notes") or "", trade_row,
                    table="trades",
                ):
                    summary.alerts_fired += 1

            row_record["state"] = state
            summary.rows.append(row_record)

        self._log.info(
            "reconcile_trades_sot.complete",
            checked=summary.checked,
            agrees=summary.agrees,
            unreconciled=summary.unreconciled,
            engine_optimistic=summary.engine_optimistic,
            polymarket_only=summary.polymarket_only,
            diverged=summary.diverged,
            paper=summary.paper,
            errors=summary.errors,
            alerts=summary.alerts_fired,
        )
        return summary

    async def _fire_sot_alert(
        self,
        trade_id,
        state: str,
        notes: str,
        row: dict,
        table: str = "manual_trades",
    ) -> bool:
        """Send a Telegram alert for an SOT divergence (deduped per table:trade_id).

        Returns True if an alert was actually sent, False if it was suppressed
        because we've already alerted on this trade_id since engine startup.

        POLY-SOT-b: ``table`` parameter namespaces the dedupe key so an alert
        on manual_trades #42 doesn't suppress one on automatic trades #42.
        Defaults to ``"manual_trades"`` for backwards compatibility with the
        existing Phase 1 call sites and tests.
        """
        dedupe_key = f"{table}:{trade_id}"
        if dedupe_key in self._sot_alerted_trade_ids:
            return False
        self._sot_alerted_trade_ids.add(dedupe_key)

        emoji = {
            "engine_optimistic": "🚨",
            "polymarket_only": "⚠️",
            "diverged": "❗",
        }.get(state, "ℹ️")
        title = state.upper().replace("_", " ")
        direction = row.get("direction", "?")
        engine_status = row.get("status", "?")
        stake = row.get("stake_usd", 0) or 0
        try:
            stake_str = f"${float(stake):.2f}"
        except (ValueError, TypeError):
            stake_str = "$?"
        # trade_id can be a string (manual_trades) or int (trades) — slice
        # only when it's a string to avoid TypeError on the int path.
        tid_display = (
            trade_id[:16] if isinstance(trade_id, str) else f"#{trade_id}"
        )
        # Tag the table so the operator knows which surface fired
        # without having to dig into the trade ID format.
        table_label = "AUTO" if table == "trades" else "MANUAL"
        msg = (
            f"{emoji} *POLY-SOT divergence: {title}*\n"
            f"{table_label} trade `{tid_display}` · {direction} · {stake_str}\n"
            f"Engine status: `{engine_status}`\n"
            f"Notes: {notes}\n"
            f"\n"
            f"_Source: SOT reconciler — Polymarket CLOB is authoritative_"
        )
        try:
            await self._alerter.send_raw_message(msg)
            self._log.warning(
                "reconcile_sot.alert_fired",
                trade_id=trade_id,
                table=table,
                state=state,
            )
            return True
        except Exception as exc:
            self._log.warning(
                "reconcile_sot.alert_failed",
                trade_id=trade_id,
                table=table,
                state=state,
                error=str(exc)[:120],
            )
            return False

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def state(self) -> ReconcilerState:
        """Current reconciler state snapshot."""
        return self._state

    @property
    def known_resolved(self) -> set[str]:
        """Set of condition IDs that have been resolved."""
        return self._known_resolved


# ─── POLY-SOT helper utilities ─────────────────────────────────────────────


def _to_float(value) -> Optional[float]:
    """Coerce a DB value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _is_paper_trade(trade: dict) -> bool:
    """POLY-SOT-d — detect whether a trade row is a paper trade.

    Paper trades never touched Polymarket so the reconciler must skip
    them. Decision order (first match wins):

      1. ``execution_mode`` column (trades table) is ``'paper'`` — this
         is the AUTHORITATIVE flag when present. Production investigation
         on 2026-04-11 confirmed some historical rows have an
         ``execution_mode='paper'`` alongside a ``0x…`` clob_order_id
         (inconsistent legacy data) and the spec explicitly says to
         TRUST execution_mode.
      2. ``mode`` column (manual_trades table) is ``'paper'``.
      3. ``order_id`` starts with ``5min-`` / ``manual-paper-`` — the
         engine's synthetic paper-run IDs.
      4. Last fallback: ``clob_order_id`` is NULL AND ``order_id`` is not
         a 0x hash — neither live path was ever taken.

    This is intentionally permissive — a false positive here just means
    a paper trade stays in the ``paper`` terminal state, which is the
    correct outcome. A false negative would surface a paper trade as
    ``engine_optimistic``, which is the bug we are fixing.
    """
    # POLY-SOT-d: execution_mode is the authoritative signal on the trades
    # table. Even when clob_order_id is populated (legacy data), trust
    # execution_mode='paper'.
    execution_mode = (trade.get("execution_mode") or "").lower()
    if execution_mode == "paper":
        return True

    order_id = trade.get("order_id") or ""
    clob_order_id = trade.get("clob_order_id")
    mode = (trade.get("mode") or "").lower()

    if isinstance(order_id, str):
        if order_id.startswith("5min-") or order_id.startswith("manual-paper-"):
            return True
        # Manual trades use `trade_id` as their string ID — defensively check
        # `trade_id` too, since the manual_trades reconciler path passes it
        # in.
        tid = trade.get("trade_id")
        if isinstance(tid, str) and tid.startswith("manual-paper-"):
            return True

    if mode == "paper":
        return True

    # Last fallback: clob_order_id NULL and order_id not a 0x hash means
    # the engine never acquired a CLOB order ID, which only happens on
    # paper runs (or on failed live orders — those are caught upstream by
    # the NULL-order-id branch, not here).
    if not clob_order_id and isinstance(order_id, str):
        if order_id and not order_id.startswith("0x"):
            return True

    return False


def _row_age_minutes(trade: dict) -> Optional[float]:
    """Return the age of a trade row in minutes, or None if no created_at.

    Uses UTC datetime arithmetic. Handles both timezone-aware and naive
    datetimes by treating naive values as UTC.
    """
    created_at = trade.get("created_at")
    if not isinstance(created_at, datetime):
        return None
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (now - created_at).total_seconds() / 60.0


class _PoolDBClient:
    """Tiny adapter that exposes the SOT helpers from `persistence.db_client`
    against an externally-managed asyncpg pool.

    The full ``DBClient`` constructs its own pool from settings; the
    reconciler is handed an existing pool by the orchestrator. Re-implementing
    the two helpers we actually need is cleaner than trying to fork DBClient.

    These mirror ``DBClient.fetch_manual_trades_for_sot_check`` and
    ``DBClient.update_manual_trade_sot`` byte-for-byte; if you change one,
    change both.
    """

    def __init__(self, pool) -> None:
        self._pool = pool

    async def fetch_manual_trades_for_sot_check(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict]:
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        trade_id,
                        polymarket_order_id,
                        status,
                        mode,
                        direction,
                        entry_price,
                        stake_usd,
                        created_at,
                        polymarket_confirmed_status,
                        polymarket_confirmed_fill_price,
                        polymarket_confirmed_size,
                        polymarket_confirmed_at,
                        polymarket_last_verified_at,
                        sot_reconciliation_state,
                        sot_reconciliation_notes
                    FROM manual_trades
                    WHERE created_at < NOW() - INTERVAL '30 seconds'
                      AND ($1::timestamptz IS NULL OR created_at >= $1)
                      AND status IN (
                          'executed', 'executing', 'open',
                          'pending_live', 'pending_paper', 'live'
                      )
                      AND (
                          sot_reconciliation_state IS NULL
                          OR sot_reconciliation_state IN ('unreconciled', 'engine_optimistic', 'diverged')
                          OR polymarket_last_verified_at IS NULL
                          OR polymarket_last_verified_at < NOW() - INTERVAL '5 minutes'
                      )
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    since,
                    int(limit),
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.warning(
                "reconcile_sot.fetch_rows_failed", error=str(exc)[:200]
            )
            return []

    async def update_manual_trade_sot(
        self,
        trade_id: str,
        *,
        polymarket_confirmed_status: Optional[str],
        polymarket_confirmed_fill_price: Optional[float],
        polymarket_confirmed_size: Optional[float],
        polymarket_confirmed_at: Optional[datetime],
        sot_reconciliation_state: str,
        sot_reconciliation_notes: Optional[str],
        polymarket_tx_hash: Optional[str] = None,
    ) -> None:
        """POLY-SOT-d: also accepts `polymarket_tx_hash` — the on-chain
        Polygon transaction hash from a matched poly_fills row. This is
        the cryptographic proof that the fill actually landed on-chain.
        NULL for every state except `agrees` / `diverged`.
        """
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE manual_trades
                    SET polymarket_confirmed_status = $1,
                        polymarket_confirmed_fill_price = $2,
                        polymarket_confirmed_size = $3,
                        polymarket_confirmed_at = $4,
                        polymarket_last_verified_at = NOW(),
                        sot_reconciliation_state = $5,
                        sot_reconciliation_notes = $6,
                        polymarket_tx_hash = $7
                    WHERE trade_id = $8
                    """,
                    polymarket_confirmed_status,
                    polymarket_confirmed_fill_price,
                    polymarket_confirmed_size,
                    polymarket_confirmed_at,
                    sot_reconciliation_state,
                    sot_reconciliation_notes,
                    polymarket_tx_hash,
                    trade_id,
                )
        except Exception as exc:
            log.warning(
                "reconcile_sot.update_row_failed",
                trade_id=trade_id,
                state=sot_reconciliation_state,
                error=str(exc)[:200],
            )

    async def fetch_manual_trades_joined_poly_fills(
        self,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> list[tuple[dict, Optional[dict]]]:
        """POLY-SOT-d: pull manual_trades rows that need reconciliation,
        LEFT JOIN'd laterally against `poly_fills`.

        Returns a list of `(trade_row, fill_row_or_None)` tuples. The fill
        row is None when no poly_fills match exists within the ±10-minute
        window. The reconciler then hands each tuple to
        ``_compare_to_polymarket_onchain`` for the terminal state decision.

        The LATERAL LEFT JOIN picks the single closest fill by
        ``ABS(match_time_utc - created_at)`` so paths 2 (exact same second)
        and 3 (±10min fuzzy) both work with one query.

        Filters:
          * created_at < NOW() - 30s (let the write settle)
          * sot_reconciliation_state IS NULL or stale
          * status includes executed/pending/live
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH normalized_trades AS (
                        SELECT
                            trade_id AS trade_id,
                            polymarket_order_id,
                            status,
                            mode,
                            direction,
                            entry_price,
                            stake_usd,
                            market_slug,
                            created_at,
                            polymarket_confirmed_status,
                            polymarket_confirmed_fill_price,
                            polymarket_confirmed_size,
                            polymarket_confirmed_at,
                            polymarket_last_verified_at,
                            sot_reconciliation_state,
                            sot_reconciliation_notes,
                            CASE
                                WHEN UPPER(direction) IN ('YES','UP')   THEN 'Up'
                                WHEN UPPER(direction) IN ('NO','DOWN')  THEN 'Down'
                                ELSE NULL
                            END AS poly_outcome
                        FROM manual_trades
                        WHERE created_at < NOW() - INTERVAL '30 seconds'
                          AND ($1::timestamptz IS NULL OR created_at >= $1)
                          AND status IN (
                              'executed', 'executing', 'open',
                              'pending_live', 'pending_paper', 'live'
                          )
                          AND (
                              sot_reconciliation_state IS NULL
                              OR sot_reconciliation_state IN (
                                  'unreconciled', 'engine_optimistic', 'diverged'
                              )
                              OR polymarket_last_verified_at IS NULL
                              OR polymarket_last_verified_at < NOW() - INTERVAL '5 minutes'
                          )
                        ORDER BY created_at DESC
                        LIMIT $2
                    )
                    SELECT
                        nt.*,
                        pf.transaction_hash AS pf_transaction_hash,
                        pf.price            AS pf_price,
                        pf.size             AS pf_size,
                        pf.cost_usd         AS pf_cost_usd,
                        pf.match_time_utc   AS pf_match_time_utc,
                        pf.condition_id     AS pf_condition_id,
                        pf.outcome          AS pf_outcome,
                        pf.side             AS pf_side
                    FROM normalized_trades nt
                    LEFT JOIN LATERAL (
                        SELECT transaction_hash, price, size, cost_usd,
                               match_time_utc, condition_id, outcome, side
                        FROM poly_fills
                        WHERE market_slug = nt.market_slug
                          AND outcome = nt.poly_outcome
                          AND side = 'BUY'
                          AND match_time_utc BETWEEN nt.created_at - INTERVAL '10 minutes'
                                                 AND nt.created_at + INTERVAL '10 minutes'
                        ORDER BY ABS(EXTRACT(EPOCH FROM (match_time_utc - nt.created_at)))
                        LIMIT 1
                    ) pf ON TRUE
                    ORDER BY nt.created_at DESC
                    """,
                    since,
                    int(limit),
                )

                result: list[tuple[dict, Optional[dict]]] = []
                for r in rows:
                    trade_dict = dict(r)
                    # Pull the pf_* fields out into a separate dict so the
                    # reconciler's _compare_to_polymarket_onchain can treat
                    # None-fill as a sentinel.
                    if trade_dict.get("pf_transaction_hash"):
                        fill_dict = {
                            "transaction_hash": trade_dict["pf_transaction_hash"],
                            "poly_price": trade_dict["pf_price"],
                            "poly_size": trade_dict["pf_size"],
                            "poly_cost_usd": trade_dict["pf_cost_usd"],
                            "match_time_utc": trade_dict["pf_match_time_utc"],
                            "condition_id": trade_dict["pf_condition_id"],
                            "outcome": trade_dict["pf_outcome"],
                            "side": trade_dict["pf_side"],
                        }
                    else:
                        fill_dict = None
                    # Keep only the trade columns in the trade_dict.
                    for k in (
                        "pf_transaction_hash",
                        "pf_price",
                        "pf_size",
                        "pf_cost_usd",
                        "pf_match_time_utc",
                        "pf_condition_id",
                        "pf_outcome",
                        "pf_side",
                    ):
                        trade_dict.pop(k, None)
                    result.append((trade_dict, fill_dict))
                return result
        except Exception as exc:
            log.warning(
                "reconcile_sot.fetch_joined_failed",
                error=str(exc)[:200],
            )
            return []


class _TradesPoolDBClient:
    """POLY-SOT-b adapter — same shape as ``_PoolDBClient`` but for the
    automatic ``trades`` table instead of operator ``manual_trades``.

    The trades table uses ``id`` (integer primary key) and stores its
    Polymarket order ID in ``clob_order_id`` (added by the v8 migration).
    POLY-SOT-b adds a parallel ``polymarket_order_id`` column to the same
    table — the SELECT here ``COALESCE``s both so historical rows that
    have only the v8 column still flow through the reconciler.

    Mirrors ``DBClient.fetch_trades_for_sot_check`` and
    ``DBClient.update_trade_sot`` byte-for-byte.
    """

    def __init__(self, pool) -> None:
        self._pool = pool

    async def fetch_trades_for_sot_check(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict]:
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        id AS trade_id,
                        order_id,
                        COALESCE(polymarket_order_id, clob_order_id) AS polymarket_order_id,
                        status,
                        mode,
                        direction,
                        entry_price,
                        stake_usd,
                        fill_price,
                        fill_size,
                        created_at,
                        is_live,
                        polymarket_confirmed_status,
                        polymarket_confirmed_fill_price,
                        polymarket_confirmed_size,
                        polymarket_confirmed_at,
                        polymarket_last_verified_at,
                        sot_reconciliation_state,
                        sot_reconciliation_notes
                    FROM trades
                    WHERE created_at < NOW() - INTERVAL '30 seconds'
                      AND ($1::timestamptz IS NULL OR created_at >= $1)
                      AND COALESCE(is_live, FALSE) = TRUE
                      AND status IN (
                          'FILLED', 'OPEN', 'PENDING', 'MATCHED',
                          'filled', 'open', 'pending', 'matched',
                          'EXPIRED', 'expired'
                      )
                      AND (
                          sot_reconciliation_state IS NULL
                          OR sot_reconciliation_state IN ('unreconciled', 'engine_optimistic', 'diverged')
                          OR polymarket_last_verified_at IS NULL
                          OR polymarket_last_verified_at < NOW() - INTERVAL '5 minutes'
                      )
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    since,
                    int(limit),
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.warning(
                "reconcile_trades_sot.fetch_rows_failed", error=str(exc)[:200]
            )
            return []

    async def update_trade_sot(
        self,
        trade_id,
        *,
        polymarket_confirmed_status: Optional[str],
        polymarket_confirmed_fill_price: Optional[float],
        polymarket_confirmed_size: Optional[float],
        polymarket_confirmed_at: Optional[datetime],
        sot_reconciliation_state: str,
        sot_reconciliation_notes: Optional[str],
        polymarket_tx_hash: Optional[str] = None,
    ) -> None:
        """POLY-SOT-d: also accepts `polymarket_tx_hash` — the Polygon
        on-chain tx hash stamped from the matched poly_fills row.
        """
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE trades
                    SET polymarket_confirmed_status = $1,
                        polymarket_confirmed_fill_price = $2,
                        polymarket_confirmed_size = $3,
                        polymarket_confirmed_at = $4,
                        polymarket_last_verified_at = NOW(),
                        sot_reconciliation_state = $5,
                        sot_reconciliation_notes = $6,
                        polymarket_tx_hash = $7
                    WHERE id = $8
                    """,
                    polymarket_confirmed_status,
                    polymarket_confirmed_fill_price,
                    polymarket_confirmed_size,
                    polymarket_confirmed_at,
                    sot_reconciliation_state,
                    sot_reconciliation_notes,
                    polymarket_tx_hash,
                    int(trade_id),
                )
        except Exception as exc:
            log.warning(
                "reconcile_trades_sot.update_row_failed",
                trade_id=trade_id,
                state=sot_reconciliation_state,
                error=str(exc)[:200],
            )

    async def fetch_trades_joined_poly_fills(
        self,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> list[tuple[dict, Optional[dict]]]:
        """POLY-SOT-d: pull trades rows that need reconciliation, LEFT JOIN
        laterally against `poly_fills`.

        Returns `(trade_row, fill_row_or_None)` tuples. Mirrors
        ``fetch_manual_trades_joined_poly_fills`` but uses the `trades`
        table (integer `id` PK, uppercase status alphabet, `is_live`
        column, and direction in YES/NO/UP/DOWN).

        The JOIN key is `trades.market_slug` — the same string stamped
        by the automatic strategies and the poly_fills_reconciler worker.
        Direction mapping is done inline via the `poly_outcome` column in
        the CTE.

        The status filter intentionally does NOT require is_live=TRUE —
        paper trades need to flow through too so the reconciler can tag
        them `paper`. The decide function short-circuits on paper before
        touching the JOIN result.
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH normalized_trades AS (
                        SELECT
                            id AS trade_id,
                            order_id,
                            clob_order_id,
                            COALESCE(polymarket_order_id, clob_order_id) AS polymarket_order_id,
                            status,
                            mode,
                            direction,
                            execution_mode,
                            is_live,
                            entry_price,
                            stake_usd,
                            fill_price,
                            fill_size,
                            market_slug,
                            created_at,
                            polymarket_confirmed_status,
                            polymarket_confirmed_fill_price,
                            polymarket_confirmed_size,
                            polymarket_confirmed_at,
                            polymarket_last_verified_at,
                            sot_reconciliation_state,
                            sot_reconciliation_notes,
                            CASE
                                WHEN UPPER(direction) IN ('YES','UP')   THEN 'Up'
                                WHEN UPPER(direction) IN ('NO','DOWN')  THEN 'Down'
                                ELSE NULL
                            END AS poly_outcome
                        FROM trades
                        WHERE created_at < NOW() - INTERVAL '30 seconds'
                          AND ($1::timestamptz IS NULL OR created_at >= $1)
                          AND status IN (
                              'FILLED', 'OPEN', 'PENDING', 'MATCHED',
                              'filled', 'open', 'pending', 'matched',
                              'EXPIRED', 'expired',
                              'RESOLVED_WIN', 'RESOLVED_LOSS',
                              'resolved_win', 'resolved_loss'
                          )
                          AND (
                              sot_reconciliation_state IS NULL
                              OR sot_reconciliation_state IN (
                                  'unreconciled', 'engine_optimistic', 'diverged'
                              )
                              OR polymarket_last_verified_at IS NULL
                              OR polymarket_last_verified_at < NOW() - INTERVAL '5 minutes'
                          )
                        ORDER BY created_at DESC
                        LIMIT $2
                    )
                    SELECT
                        nt.*,
                        pf.transaction_hash AS pf_transaction_hash,
                        pf.price            AS pf_price,
                        pf.size             AS pf_size,
                        pf.cost_usd         AS pf_cost_usd,
                        pf.match_time_utc   AS pf_match_time_utc,
                        pf.condition_id     AS pf_condition_id,
                        pf.outcome          AS pf_outcome,
                        pf.side             AS pf_side
                    FROM normalized_trades nt
                    LEFT JOIN LATERAL (
                        SELECT transaction_hash, price, size, cost_usd,
                               match_time_utc, condition_id, outcome, side
                        FROM poly_fills
                        WHERE market_slug = nt.market_slug
                          AND outcome = nt.poly_outcome
                          AND side = 'BUY'
                          AND match_time_utc BETWEEN nt.created_at - INTERVAL '10 minutes'
                                                 AND nt.created_at + INTERVAL '10 minutes'
                        ORDER BY ABS(EXTRACT(EPOCH FROM (match_time_utc - nt.created_at)))
                        LIMIT 1
                    ) pf ON TRUE
                    ORDER BY nt.created_at DESC
                    """,
                    since,
                    int(limit),
                )
                result: list[tuple[dict, Optional[dict]]] = []
                for r in rows:
                    trade_dict = dict(r)
                    if trade_dict.get("pf_transaction_hash"):
                        fill_dict = {
                            "transaction_hash": trade_dict["pf_transaction_hash"],
                            "poly_price": trade_dict["pf_price"],
                            "poly_size": trade_dict["pf_size"],
                            "poly_cost_usd": trade_dict["pf_cost_usd"],
                            "match_time_utc": trade_dict["pf_match_time_utc"],
                            "condition_id": trade_dict["pf_condition_id"],
                            "outcome": trade_dict["pf_outcome"],
                            "side": trade_dict["pf_side"],
                        }
                    else:
                        fill_dict = None
                    for k in (
                        "pf_transaction_hash",
                        "pf_price",
                        "pf_size",
                        "pf_cost_usd",
                        "pf_match_time_utc",
                        "pf_condition_id",
                        "pf_outcome",
                        "pf_side",
                    ):
                        trade_dict.pop(k, None)
                    result.append((trade_dict, fill_dict))
                return result
        except Exception as exc:
            log.warning(
                "reconcile_trades_sot.fetch_joined_failed",
                error=str(exc)[:200],
            )
            return []
