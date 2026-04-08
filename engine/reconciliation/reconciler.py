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
from datetime import datetime, timezone
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
    from execution.polymarket_client import PolymarketClient

log = structlog.get_logger(__name__)


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
    ) -> None:
        self._poly = poly_client
        self._pool = db_pool
        self._alerter = alerter
        self._shutdown = shutdown_event
        self._poll_interval = poll_interval
        self._report_interval = report_interval

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

                # Try to match and update unresolved trades
                try:
                    async with self._pool.acquire() as conn:
                        # Match by token_id in metadata
                        match = await conn.fetchrow(
                            """SELECT id, metadata->>'token_id' as token_id,
                                      metadata->>'entry_reason' as reason
                               FROM trades
                               WHERE outcome IS NULL
                                 AND is_live = true
                                 AND metadata->>'token_id' IS NOT NULL
                               ORDER BY created_at DESC LIMIT 20""",
                        )

                        if match:
                            token_id = match["token_id"]
                            pnl = data["pnl"] if outcome == "WIN" else -data["cost"]
                            status = (
                                "RESOLVED_WIN" if outcome == "WIN" else "RESOLVED_LOSS"
                            )
                            updated = await conn.execute(
                                """UPDATE trades SET outcome = $1, pnl_usd = $2,
                                          resolved_at = NOW(), status = $3
                                   WHERE metadata->>'token_id' = $4
                                     AND outcome IS NULL""",
                                outcome,
                                pnl,
                                status,
                                token_id,
                            )
                            if "UPDATE" in str(updated):
                                backfilled += 1
                                self._log.info(
                                    "reconciler.backfill.updated",
                                    condition_id=cid[:20],
                                    token_id=token_id[:20] if token_id else "?",
                                    outcome=outcome,
                                )
                        else:
                            orphaned += 1
                            self._log.debug(
                                "reconciler.backfill.orphaned",
                                condition_id=cid[:20],
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
                    token_id=data.get("tokenId", ""),
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
        pos_by_token: dict[str, dict] = {}
        for _cid, pdata in positions.items():
            tid = pdata.get("tokenId", "")
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
            shares = float(orphan["shares_filled"] or 0) if orphan["shares_filled"] else fill_size

            if pos_outcome not in ("WIN", "LOSS"):
                # Market hasn't resolved yet — skip for now
                continue

            # Position outcome is already WIN/LOSS from get_position_outcomes()
            # WIN means curPrice >= 0.99 (token pays $1), LOSS means curPrice <= 0.01
            is_win = pos_outcome == "WIN"
            if is_win:
                outcome = "WIN"
                pnl = shares - cost  # shares pay out $1 each on win
                status = "RESOLVED_WIN"
            else:
                outcome = "LOSS"
                pnl = -cost
                status = "RESOLVED_LOSS"

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
                pnl_str = f"+${pnl:.2f}" if is_win else f"-${cost:.2f}"
                emoji = "WIN" if is_win else "LOSS"
                reason_line = ""
                if orphan["entry_reason"]:
                    reason_line = f"Entry: `{orphan['entry_reason']}`\n"

                trade_direction = (orphan["direction"] or "YES").upper()
                msg = (
                    f"*{emoji} -- ORPHAN RESOLVED* (GTC fill)\n"
                    f"`{now_str}`\n"
                    f"\n"
                    f"Direction: `{trade_direction}`\n"
                    f"Resolution: `{pos_outcome}`\n"
                    f"Shares: `{shares:.2f}` @ `${fill_price:.4f}`\n"
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
        _pos_token_id = str(data.get("tokenId", ""))

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

                    # Prefix match fallback: token_id may differ by 1 char in length
                    if not match and _pos_token_id and len(_pos_token_id) > 10:
                        match = await conn.fetchrow(
                            """SELECT id, metadata->>'entry_reason' as reason,
                                      metadata->>'token_id' as token_id
                               FROM trades
                               WHERE is_live = true
                                 AND outcome IS NULL
                                 AND metadata->>'token_id' IS NOT NULL
                                 AND (metadata->>'token_id' LIKE $1 || '%'
                                      OR $2 LIKE metadata->>'token_id' || '%')
                               ORDER BY created_at DESC LIMIT 1""",
                            _pos_token_id[:60],
                            _pos_token_id,
                        )
                        if match:
                            self._log.info(
                                "reconciler.prefix_match",
                                pos_token=_pos_token_id[:20],
                                db_token=(match["token_id"] or "")[:20],
                            )

                    if match:
                        matched_trade_id = match["id"]
                        matched_reason = match["reason"]
                        matched_token_id = match["token_id"]

                        await conn.execute(
                            """UPDATE trades SET outcome = $1, pnl_usd = $2,
                                      resolved_at = NOW(),
                                      status = CASE WHEN $1 = 'WIN'
                                               THEN 'RESOLVED_WIN'
                                               ELSE 'RESOLVED_LOSS' END
                               WHERE id = $3 AND outcome IS NULL""",
                            outcome,
                            pnl,
                            matched_trade_id,
                        )
                        self._log.info(
                            "reconciler.trade_resolved",
                            trade_id=matched_trade_id,
                            token_id=matched_token_id[:20] if matched_token_id else "?",
                            outcome=outcome,
                            pnl=f"${pnl:.2f}",
                        )
                    else:
                        self._log.warning(
                            "reconciler.no_trade_match",
                            condition_id=condition_id[:20],
                            pos_token=_pos_token_id[:20] if _pos_token_id else "?",
                            cost=f"${cost:.2f}",
                            outcome=outcome,
                        )
            except Exception as exc:
                self._log.warning(
                    "reconciler.resolve_db_error",
                    condition_id=condition_id[:20],
                    error=str(exc)[:100],
                )

        # Send Telegram notification
        try:
            if outcome == "WIN":
                emoji = "WIN"
                pnl_str = f"+${pnl:.2f}"
            else:
                emoji = "LOSS"
                pnl_str = f"-${cost:.2f}"

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
                f"Shares: `{size:.2f}`\n"
                f"Avg Price: `${avg_price:.4f}`\n"
                f"Cost: `${cost:.2f}`\n"
                f"P&L: `{pnl_str}`\n"
                f"{reason_line}"
                f"{wallet_str}\n"
                f"\n"
                f"_Source: CLOB Reconciler_"
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
