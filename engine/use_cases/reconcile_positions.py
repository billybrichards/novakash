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
the two SOT passes. Called every 2 minutes. Live path runs unconditionally
when ``not paper_mode``; paper path always runs.

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
from typing import Any, Optional

from alerts.haiku_summarizer import HaikuSummarizer
from domain.ports import (
    TradeRepository,
    WindowStateRepository,
)
from use_cases.ports import AlerterPort, Clock
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
        self._haiku = HaikuSummarizer()
        # Pending per-resolution alert payloads, flushed as one batched
        # Telegram summary at the end of each execute() call. Replaces
        # the old per-position spam (10+ messages per reconcile pass).
        self._pending_live_alerts: list[dict] = []
        self._pending_paper_alerts: list[dict] = []

        # 2026-04-17 fix: session-level dedupe for per-trade WIN/LOSS alerts.
        # The ``WHERE outcome IS NULL`` filter used to be the dedupe mechanism
        # (a trade could only be matched once, before its outcome got set).
        # After dropping that filter from token_id / prefix queries — required
        # so reconciler can find trades that the CLOBReconciler startup
        # backfill silently resolved — we need an explicit dedupe so the same
        # trade doesn't fire an alert on every reconciler pass.
        self._alerted_trade_ids: set[str] = set()

    async def execute(
        self,
        positions: list[PositionOutcome],
    ) -> ReconcileResult:
        """Run live (CLOB) and paper (oracle) resolution in a single pass.

        ``positions`` — list of PositionOutcome from poly_client.get_position_outcomes().
        Pass ``[]`` in paper mode (no real CLOB positions exist).
        """
        # Reset pending alert buffers — fresh summary per execute() pass.
        self._pending_live_alerts = []
        self._pending_paper_alerts = []

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

        # Oracle poll FIRST: stamp oracle_outcome from Polymarket Gamma on any
        # window closed in the last 15 min lacking an oracle label. Labeler
        # below then prefers oracle_outcome over the chainlink/delta fallbacks.
        try:
            await self._window_state.populate_oracle_outcomes()
        except Exception as exc:
            logger.warning("reconciler.oracle_poll_error", error=str(exc)[:100])

        # Shadow-label pass: stamp actual_direction on ALL resolved windows so
        # paper trades can look up their outcome.
        windows_labeled = 0
        try:
            windows_labeled = await self._window_state.label_resolved_windows()
        except Exception as exc:
            logger.warning("reconciler.label_windows_error", error=str(exc)[:100])

        paper_resolved, paper_skipped, paper_errors = await self._resolve_paper_batch()

        # Flush batched Telegram summary (one message for LIVE + paper).
        try:
            await self._flush_resolution_alerts()
        except Exception as exc:
            logger.debug("reconciler.flush_alerts_failed", error=str(exc)[:120])

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
                    await self._send_resolution_alert(
                        trade=trade,
                        outcome=outcome,
                        pnl=pnl,
                        actual_direction=actual_direction,
                        stake=stake,
                        shares=shares,
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
                direction=None,
                window_ts=None,
                match_method=None,
                strategy=None,
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

        # Phantom-trade guard (all tiers): refuse to score trades that
        # have no on-chain evidence AND are from phantom-prone exec modes.
        # Hub note #147: gtc_resting/gtc phantom trades poisoned P&L.
        tx_hash = (match.get("polymarket_tx_hash") or "").strip()
        exec_mode = match.get("execution_mode") or ""
        if not tx_hash and exec_mode in ("gtc_resting", "gtc"):
            logger.warning(
                "reconciler.phantom_trade_skip",
                extra={
                    "trade_id": trade_id,
                    "execution_mode": exec_mode,
                    "reason": "no polymarket_tx_hash on gtc/gtc_resting trade",
                },
            )
            return None

        # 2026-04-17 fix: skip the DB resolve_trade write if the match
        # already has an outcome set (e.g. resolved silently by the
        # CLOBReconciler startup backfill). Otherwise we'd re-stomp
        # resolved_at on every reconciler pass. Still fall through to
        # the alert path so the user sees a per-trade WIN/LOSS message.
        already_resolved = bool(match.get("outcome"))

        if not already_resolved:
            await self._trade_repo.resolve_trade(
                trade_id=trade_id,
                outcome=outcome,
                pnl_usd=trade_pnl,
                status=status,
            )

        # Session-level dedupe — only alert once per trade_id per engine
        # session. Backfilled trades fire on their first reconciler-seen
        # pass; steady-state trades fire on their normal first resolve.
        trade_id_key = str(trade_id)
        if trade_id_key in self._alerted_trade_ids:
            logger.debug(
                "reconciler.trade_alert_suppressed_duplicate",
                extra={"trade_id": trade_id, "outcome": outcome},
            )
            return None
        self._alerted_trade_ids.add(trade_id_key)

        logger.info(
            "reconciler.trade_resolved",
            extra={
                "trade_id": trade_id,
                "token_id": (match.get("token_id") or "?")[:20],
                "outcome": outcome,
                "pnl": f"${trade_pnl:.2f}",
                "match_method": match_method,
                "already_resolved_in_db": already_resolved,
            },
        )

        # Mark resolved in window state (non-fatal if this fails)
        try:
            asset = match.get("asset", "BTC")
            window_ts = match.get("window_ts")
            if window_ts:
                # v4.4.0: infer oracle-resolved direction from trade direction
                # + outcome so window_states.actual_direction is populated for
                # downstream skip-outcome analysis. Inference:
                #   trade UP   + WIN  → market UP   (signal correct)
                #   trade UP   + LOSS → market DOWN (signal wrong)
                #   trade DOWN + WIN  → market DOWN
                #   trade DOWN + LOSS → market UP
                trade_dir = (match.get("direction") or "").upper()
                actual_direction: Optional[str] = None
                if trade_dir in ("UP", "DOWN"):
                    won = outcome == "WIN"
                    if trade_dir == "UP":
                        actual_direction = "UP" if won else "DOWN"
                    else:
                        actual_direction = "DOWN" if won else "UP"
                await self._window_state.mark_resolved(
                    WindowKey(asset=asset, window_ts=int(window_ts)),
                    WindowOutcome(
                        outcome=outcome,
                        pnl_usd=trade_pnl,
                        resolved_at=self._clock.now(),
                        actual_direction=actual_direction,
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
            direction=match.get("direction"),
            window_ts=match.get("window_ts"),
            match_method=match_method,
            strategy=match.get("strategy"),
        )

        return ResolutionResult(
            condition_id=position.condition_id,
            matched_trade_id=trade_id,
            outcome=status,  # "RESOLVED_WIN" | "RESOLVED_LOSS" per domain contract
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
                # Phantom-trade guard (2026-04-17 incident #4881). The
                # cost-fallback match runs AFTER exact + prefix token_id
                # tiers have failed, meaning the on-chain position cannot
                # be tied to a local trade via its actual identifier. In
                # the #4881 incident, this tier fake-resolved trades whose
                # orders were never placed on-chain (synthetic order_ids
                # from a stale paper-mode polymarket_client after a mode
                # switch) by matching them to unrelated on-chain positions
                # that happened to have a similar cost basis.
                #
                # Safety: refuse the match when the local trade has NO
                # on-chain evidence. A trade with no polymarket_tx_hash
                # AND a paper-prefixed / synthetic polymarket_order_id
                # CANNOT have produced this on-chain position. Return
                # None so the position is handled by a later sweep that
                # knows to mark the trade ``unreconciled_no_onchain``.
                tx_hash = (match.get("polymarket_tx_hash") or "").strip()
                order_id = (match.get("polymarket_order_id") or "").strip()
                is_synthetic = (
                    not order_id
                    or order_id.startswith("paper-")
                    or order_id.startswith("fak-")
                    or order_id.startswith("fok-")
                    or order_id.startswith("gtc-")
                )
                if not tx_hash and is_synthetic:
                    logger.warning(
                        "reconciler.cost_fallback_rejected",
                        extra={
                            "trade_id": match.get("id"),
                            "cost": f"${position.cost:.2f}",
                            "condition_id": position.condition_id[:20],
                            "reason": (
                                "no_onchain_evidence: trade has no "
                                "polymarket_tx_hash and order_id is "
                                "synthetic; refusing to cost-fallback "
                                "match against unrelated on-chain position"
                            ),
                        },
                    )
                    return None, None
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
        direction: Optional[str] = None,
        window_ts: Optional[int] = None,
        match_method: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> None:
        """Queue a live position resolution for the batched end-of-pass summary.

        Previously sent one Telegram message per position — N positions
        in one reconcile pass = N spammed messages (see audit). Now
        appends to ``self._pending_live_alerts`` and the final batched
        summary is emitted by ``_flush_resolution_alerts``.

        v4.4.0 (2026-04-16): now carries trade direction + entry + window_ts
        + match_method + strategy so the flushed message can show proper
        per-trade detail and orphans can be separated from real LOSSES.
        """
        alert_dict = {
            "outcome": position.outcome,
            "pnl": float(pnl),
            "cost": float(cost),
            "entry_price": float(entry_price) if entry_price else None,
            "matched": matched_trade_id is not None,
            "condition_id": position.condition_id,
            "direction": (direction or "").upper() or None,
            "window_ts": int(window_ts) if window_ts else None,
            "match_method": match_method,
            "strategy": strategy,
        }
        self._pending_live_alerts.append(alert_dict)

        # Emit rich per-trade v2 card (in addition to batched summary)
        if alert_dict["matched"] and hasattr(self._alerts, "emit_per_trade_resolved_v2"):
            try:
                await self._alerts.emit_per_trade_resolved_v2(
                    direction=alert_dict["direction"] or "UP",
                    outcome=position.outcome,
                    pnl=float(pnl),
                    entry_price=float(entry_price) if entry_price else 0.50,
                    cost=float(cost),
                    window_ts=int(window_ts) if window_ts else 0,
                    strategy=strategy or "unknown",
                )
            except Exception as exc:
                logger.warning(
                    "reconcile.per_trade_resolved_v2_failed",
                    error=str(exc)[:200],
                )

    async def _send_resolution_alert(
        self,
        trade: dict,
        outcome: str,
        pnl: float,
        actual_direction: str,
        stake: float,
        shares: float,
    ) -> None:
        """Queue a paper-trade resolution for the batched end-of-pass summary.

        Previously invoked ``HaikuSummarizer.summarize_resolution`` per
        trade, which (a) fired a separate Telegram message per trade
        (spam) and (b) burned one Haiku API call per trade. Now appends
        to ``self._pending_paper_alerts`` and ``_flush_resolution_alerts``
        emits one batched message.
        """
        self._pending_paper_alerts.append(
            {
                "strategy": trade.get("strategy") or "unknown",
                "direction": (trade.get("direction") or "?").upper(),
                "outcome": outcome,
                "pnl": float(pnl),
                "stake": float(stake),
                "shares": float(shares),
                "actual_direction": (actual_direction or "?").upper(),
            }
        )

    async def _flush_resolution_alerts(self) -> None:
        """Emit ONE batched Telegram summary of this pass's resolutions.

        Format (v4.4.0 — matched vs orphan separated so pre-fix legacy
        positions don't inflate the LOSS count):

            *Reconcile pass*
            LIVE matched: 2 WIN / 1 LOSS (net +$4.72)
              ✅ WIN  UP  @ $0.55  +$4.17  cost $4.86  06:35 ET
              ✅ WIN  UP  @ $0.54  +$3.84  cost $4.86  06:36 ET
              ❌ LOSS UP  @ $0.51  -$4.86  cost $4.86  06:51 ET
            Orphans: 5 (pre-#211 legacy, no DB match)
              3 auto-redeemed wins, 2 worthless tokens
            Paper: 3 WIN / 1 LOSS (net +$1.80)

        Matched P&L is the ONLY authoritative number — orphans were
        already settled on-chain (NegRisk auto-redeem) and their USDC is
        in the wallet; they just can't be matched to trade rows because
        they predate PR #211's ``strategy_id`` fix.
        """
        live = self._pending_live_alerts
        paper = self._pending_paper_alerts
        if not live and not paper:
            return

        lines: list[str] = ["*Reconcile pass*"]

        if live:
            matched = [a for a in live if a["matched"]]
            orphans = [a for a in live if not a["matched"]]

            # Matched: these are REAL resolved trades — count toward net
            if matched:
                m_wins = [a for a in matched if a["outcome"] == "WIN"]
                m_losses = [a for a in matched if a["outcome"] == "LOSS"]
                m_net = sum(a["pnl"] for a in matched)
                net_str = (
                    f"+${m_net:.2f}" if m_net >= 0 else f"-${abs(m_net):.2f}"
                )
                lines.append(
                    f"LIVE matched: {len(m_wins)} WIN / {len(m_losses)} LOSS "
                    f"(net {net_str})"
                )
                detail_cap = 10
                for a in matched[:detail_cap]:
                    emoji = "✅" if a["outcome"] == "WIN" else "❌"
                    pnl_str = (
                        f"+${a['pnl']:.2f}"
                        if a["pnl"] >= 0
                        else f"-${abs(a['pnl']):.2f}"
                    )
                    direction = a.get("direction") or "?"
                    entry = a.get("entry_price")
                    entry_str = f"@ ${entry:.3f}" if entry else ""
                    # Window time — HH:MM UTC
                    window_ts = a.get("window_ts")
                    if window_ts:
                        from datetime import datetime, timezone

                        wt = datetime.fromtimestamp(
                            int(window_ts), tz=timezone.utc
                        ).strftime("%H:%M")
                        wt_str = f" {wt} UTC"
                    else:
                        wt_str = ""
                    lines.append(
                        f"  {emoji} {a['outcome']:4s} {direction:4s} {entry_str:<12s} "
                        f"{pnl_str:>8s}  cost ${a['cost']:.2f}{wt_str}"
                    )
                if len(matched) > detail_cap:
                    lines.append(f"  … +{len(matched) - detail_cap} more")

            # Orphans: pre-fix legacy positions — NOT counted as LOSS
            if orphans:
                o_wins = sum(1 for a in orphans if a["outcome"] == "WIN")
                o_losses = sum(1 for a in orphans if a["outcome"] == "LOSS")
                lines.append(
                    f"Orphans: {len(orphans)} (pre-#211 legacy, no DB match)"
                )
                lines.append(
                    f"  _{o_wins} auto-redeemed wins, "
                    f"{o_losses} worthless tokens — USDC already settled_"
                )

        if paper:
            wins = [a for a in paper if a["outcome"] == "WIN"]
            losses = [a for a in paper if a["outcome"] == "LOSS"]
            net = sum(a["pnl"] for a in paper)
            lines.append(
                f"Paper: {len(wins)} WIN / {len(losses)} LOSS "
                f"(net {'+' if net >= 0 else ''}${net:.2f})"
            )
            detail_cap = 8
            for a in paper[:detail_cap]:
                emoji = "✅" if a["outcome"] == "WIN" else "❌"
                pnl_str = (
                    f"+${a['pnl']:.2f}" if a["pnl"] >= 0 else f"-${abs(a['pnl']):.2f}"
                )
                lines.append(
                    f"  {emoji} {a['strategy']} {a['direction']} "
                    f"{pnl_str} (stake ${a['stake']:.2f})"
                )
            if len(paper) > detail_cap:
                lines.append(f"  … +{len(paper) - detail_cap} more")

        msg = "\n".join(lines)

        if hasattr(self._alerts, "send_raw_message"):
            await self._alerts.send_raw_message(msg)
        elif hasattr(self._alerts, "send_system_alert"):
            await self._alerts.send_system_alert(msg)

        # ── Narrative V2 dual-fire (Phase G.3) ────────────────────────────────
        # Emit a parallel ReconcilePayload that (a) dedupes orphan reporting
        # across passes and (b) groups matched rows by (timeframe, strategy).
        if hasattr(self._alerts, "emit_reconcile_v2"):
            try:
                await self._alerts.emit_reconcile_v2(
                    live_alerts=live,
                    paper_alerts=paper,
                )
            except Exception as exc:
                logger.warning(
                    "reconcile.narrative_v2_emit_failed",
                    error=str(exc)[:200],
                )
