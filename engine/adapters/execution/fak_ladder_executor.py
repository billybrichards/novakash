"""FAK Ladder Executor -- live execution via FAK ladder -> RFQ -> GTC fallback.

Wraps the existing FOKLadder + PolymarketClient.place_rfq_order +
PolymarketClient.place_order into a single execute_order() call that
satisfies the OrderExecutionPort interface.

This adapter owns the multi-step execution strategy. The use case just
calls execute_order() and gets back an ExecutionResult.

GTC lifecycle hardening (2026-05-10):
- Dedup: max 1 GTC per (token_id, side) — a second attempt for the same
  window+direction is blocked with failure_reason='gtc_dedup_blocked'.
- Auto-cancel: call cancel_expired_gtc_orders(expired_token_ids) from the
  engine heartbeat (or window resolver) to cancel resting GTCs for windows
  that have closed without a fill, preventing phantom fills on expired markets.

Audit: SP-06 Phase 4.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from decimal import Decimal
from typing import Any, Optional, TypedDict

from use_cases.ports.execution import OrderExecutionPort
from domain.ports import PolymarketClientPort
from domain.value_objects import ExecutionResult

logger = logging.getLogger(__name__)

# Polymarket binary options fee
FEE_MULTIPLIER = 0.072

# GTC poll config
DEFAULT_GTC_POLL_INTERVAL = 5
# 2026-05-14: raised 60 → 180s. With FAK ladder up to $0.92 cap, GTC
# fallback should be allowed more wall-clock to rest and fill before
# expiring.
DEFAULT_GTC_MAX_WAIT = 180

# Pi bonus for GTC after FAK exhaustion
DEFAULT_PI_BONUS = 0.0314

# Seconds after window close before a resting GTC is considered expired.
# 30s grace ensures we don't cancel during the final-fill window when the
# market is still resolving. Cancel loop fires every 30s — worst-case a
# resting GTC can survive for (300 + 30 + 30) ≈ 6 min before expiry.
GTC_EXPIRY_GRACE_SECONDS = 30

# Each 5-min window is 300 seconds.
_WINDOW_DURATION_SECONDS = 300


class _GTCEntry(TypedDict):
    """Registry entry for one resting GTC order."""

    order_id: str
    placed_at: float   # wall-clock time.time() at placement
    close_ts: float    # estimated window close (placed_at rounded up to next 300s boundary)

# Phase-3 GTC fallback default. Disabled (False) post-2026-04-17 incident:
# 20/20 overnight gtc_resting orders booked as RESOLVED_LOSS in trades table
# but never matched a poly_fills row on-chain (engine_optimistic state) —
# -$77.90 phantom loss. See Hub note 64, audit-task #218.
# Set FAK_LADDER_ENABLE_GTC=true (case-insensitive 1/true/yes) to re-enable
# the legacy Phase-3 fallback for back-compat / experimentation.
_DEFAULT_ENABLE_GTC_FALLBACK = False

# Phase-2 RFQ gate. Enabled by default — RFQ (market-maker fill) is the
# intended fallback when FAK ladder exhausts. Disable via
# FAK_LADDER_ENABLE_RFQ=false when the RFQ endpoint is returning 404s
# (e.g. token_id lookup breaking against the CLOB markets list) or during
# incident investigation. With both RFQ and GTC disabled the executor
# becomes a strict FAK-only path; windows where FAK cannot fill will
# skip cleanly (no phantom rows, no orphan fills).
_DEFAULT_ENABLE_RFQ = True

# ── FAK ladder total-elapsed timeout (v6 late-fill defence, 2026-04-21) ──
# Incident: window 1776803100, order 0x9cb301f2 filled at T-16 (16s before
# window close) because a FAK ladder started at T-68 kept retrying even
# when wall-clock had drifted past the strategy's min_offset_sec=30 gate.
# A hard total-elapsed cap on the entire execute_order() call bounds the
# damage regardless of which phase (FAK / RFQ / GTC poll) is running.
# Default 20s — well under any 5m-window min_offset_sec (>=30s) — so the
# ladder cannot chew through enough wall-clock to fill after the timing
# guard should have fired. Override with FAK_LADDER_MAX_ELAPSED_S.
_DEFAULT_MAX_LADDER_ELAPSED_S = 20.0


class FAKLadderExecutor(OrderExecutionPort):
    """Live execution: FAK ladder -> RFQ -> GTC fallback.

    Three-phase execution:
      Phase 1: FAK at cap -> FAK at cap + pi (2 attempts via FOKLadder)
      Phase 2: RFQ at cap (market maker fill)
      Phase 3: GTC at cap + pi bonus (resting order, poll for fill)
    """

    def __init__(
        self,
        poly_client: PolymarketClientPort,
        *,
        pi_bonus_cents: float = DEFAULT_PI_BONUS,
        gtc_poll_interval: int = DEFAULT_GTC_POLL_INTERVAL,
        gtc_max_wait: int = DEFAULT_GTC_MAX_WAIT,
        enable_gtc_fallback: Optional[bool] = None,
        enable_rfq: Optional[bool] = None,
        max_ladder_elapsed_s: Optional[float] = None,
    ) -> None:
        self._poly = poly_client
        self._pi_bonus = pi_bonus_cents
        self._gtc_poll_interval = gtc_poll_interval
        self._gtc_max_wait = gtc_max_wait
        if max_ladder_elapsed_s is None:
            env_val = os.environ.get(
                "FAK_LADDER_MAX_ELAPSED_S",
                str(_DEFAULT_MAX_LADDER_ELAPSED_S),
            )
            try:
                self._max_ladder_elapsed_s = float(env_val)
            except (TypeError, ValueError):
                self._max_ladder_elapsed_s = _DEFAULT_MAX_LADDER_ELAPSED_S
        else:
            self._max_ladder_elapsed_s = float(max_ladder_elapsed_s)
        if enable_gtc_fallback is None:
            env = os.environ.get("FAK_LADDER_ENABLE_GTC", "").strip().lower()
            self._enable_gtc_fallback = env in ("1", "true", "yes", "on")
        else:
            self._enable_gtc_fallback = bool(enable_gtc_fallback)
        # RFQ is enabled by default; disable via env for incident response.
        if enable_rfq is None:
            env = os.environ.get("FAK_LADDER_ENABLE_RFQ", "").strip().lower()
            # Blank / unset → default True. Explicit false/0/no/off → disabled.
            self._enable_rfq = env not in ("0", "false", "no", "off")
        else:
            self._enable_rfq = bool(enable_rfq)
        if not self._enable_gtc_fallback:
            logger.info(
                "fak_ladder.init",
                extra={"gtc_fallback": "disabled (default; set FAK_LADDER_ENABLE_GTC=true to re-enable)"},
            )
        if not self._enable_rfq:
            logger.info(
                "fak_ladder.init",
                extra={"rfq": "disabled (FAK_LADDER_ENABLE_RFQ=false)"},
            )

        # Always log init config so deploys can be verified in engine.log.
        logger.info(
            "fak_ladder.init",
            extra={
                "gtc_poll_interval_s": self._gtc_poll_interval,
                "gtc_max_wait_s": self._gtc_max_wait,
                "pi_bonus": self._pi_bonus,
                "max_ladder_elapsed_s": self._max_ladder_elapsed_s,
                "fak_ladder_rungs_env": os.environ.get("FAK_LADDER_RUNGS", "(unset → default 0.0,0.02,0.04,0.07)"),
                "fak_ladder_max_price_env": os.environ.get("FAK_LADDER_MAX_PRICE", "(unset → default 0.92)"),
            },
        )

        # GTC dedup registry: (token_id, side) -> _GTCEntry
        # Enforces max 1 GTC per window+direction. Entries removed on
        # cancel_expired_gtc_orders() call or when the order is confirmed filled.
        # Dict is bounded: entries are pruned on cancel/expiry. In the pathological
        # case of very many distinct token IDs without expiry calls, cap at 1000.
        # Keyed by (strategy_id, token_id, side) — per-strategy scoping
        # added 2026-05-14 so independent strats (v9_2_raw_lgb +
        # v9_2_v12_combo) don't block each other on shared windows.
        self._active_gtc: dict[tuple[str, str, str], _GTCEntry] = {}

    async def execute_order(
        self,
        token_id: str,
        side: str,
        stake_usd: float,
        entry_cap: float,
        price_floor: float,
        gtc_cap: Optional[float] = None,
        strategy_id: str = "",
    ) -> ExecutionResult:
        """Execute using the FAK -> RFQ -> GTC ladder.

        ``strategy_id`` scopes the in-memory GTC dedup registry so two
        independent strategies (e.g. v9_2_raw_lgb + v9_2_v12_combo) can
        both place their own resting orders on the same (token_id, side).
        Same-strategy retries within a window still hit the dedup as
        intended (anti-phantom-fill safeguard from 2026-04-17 incident).

        Returns ExecutionResult. Does not raise.
        """
        start = time.time()
        fak_prices: list[float] = []

        def _elapsed() -> float:
            return time.time() - start

        def _timeout_result(elapsed: float) -> ExecutionResult:
            """Build a skip-style ExecutionResult for a ladder-timeout abort.

            Intentionally uses execution_mode='none' + a clearly-prefixed
            failure_reason so dashboards + reconcilers treat this as a
            benign skip (no fill, no trade row), not a CLOB-infra error.
            Counters keyed off _REAL_ERROR_REASON_PREFIXES in
            ``execute_trade.py`` must NOT include ``fak_ladder_timeout``.
            """
            logger.warning(
                "fak_ladder.timeout",
                extra={
                    "elapsed_s": round(elapsed, 2),
                    "max_s": self._max_ladder_elapsed_s,
                    "fak_prices": fak_prices,
                },
            )
            return ExecutionResult(
                success=False,
                failure_reason=(
                    f"fak_ladder_timeout: {elapsed:.1f}s > "
                    f"{self._max_ladder_elapsed_s:.1f}s"
                ),
                stake_usd=stake_usd,
                execution_mode="none",
                fak_attempts=len(fak_prices),
                fak_prices=fak_prices,
                token_id=token_id,
                execution_start=start,
                execution_end=time.time(),
            )

        # ── Phase 1: FAK ladder ─────────────────────────────────────────
        # Pre-entry timeout check is cheap; abort-after checks bound
        # the remaining phases.
        if _elapsed() > self._max_ladder_elapsed_s:
            return _timeout_result(_elapsed())

        try:
            from execution.fok_ladder import FOKLadder

            ladder = FOKLadder(self._poly)
            fok_result = await ladder.execute(
                token_id=token_id,
                direction="BUY",
                stake_usd=stake_usd,
                max_price=entry_cap,
                min_price=price_floor,
            )
            fak_prices = fok_result.attempted_prices

            if fok_result.filled:
                fee = self._calc_fee(
                    fok_result.fill_price or entry_cap,
                    stake_usd,
                )
                return ExecutionResult(
                    success=True,
                    order_id=fok_result.order_id,
                    fill_price=fok_result.fill_price,
                    fill_size=fok_result.shares,
                    stake_usd=stake_usd,
                    fee_usd=fee,
                    execution_mode="fak",
                    fak_attempts=fok_result.attempts,
                    fak_prices=fak_prices,
                    token_id=token_id,
                    execution_start=start,
                    execution_end=time.time(),
                )

            # Surface CLOB auth/infra abort reasons so they propagate to
            # the final ExecutionResult instead of being masked as a benign
            # market-side no-fill. Checked before Phase 2/3 fallbacks.
            #
            # ``book_unavailable_404`` (audit 2026-04-26) is the orderbook
            # propagation-lag skip — not a fault, but RFQ on the same
            # token_id will also 404, so we short-circuit Phase 2/3
            # entirely and let the strategy retry on the next eval.
            if fok_result.abort_reason and (
                "clob_auth_error" in (fok_result.abort_reason or "")
                or "book_error" in (fok_result.abort_reason or "")
                or "book_unavailable_404" in (fok_result.abort_reason or "")
            ):
                return ExecutionResult(
                    success=False,
                    failure_reason=fok_result.abort_reason,
                    stake_usd=stake_usd,
                    execution_mode="none",
                    fak_attempts=fok_result.attempts,
                    fak_prices=fak_prices,
                    token_id=token_id,
                    execution_start=start,
                    execution_end=time.time(),
                )

            logger.info(
                "fak_ladder.exhausted",
                extra={
                    "attempts": fok_result.attempts,
                    "prices": fak_prices,
                    "abort": fok_result.abort_reason,
                },
            )
        except Exception as exc:
            logger.warning(
                "fak_ladder.error",
                extra={"error": str(exc)[:200]},
            )

        # Post-phase-1 timeout check — FAK ladder's internal retry sleep
        # plus two attempts can easily burn 10-15s even when each attempt
        # is quick. Aborting here means RFQ + GTC fallbacks don't stack
        # more wall-clock on top of an already-slow ladder.
        if _elapsed() > self._max_ladder_elapsed_s:
            return _timeout_result(_elapsed())

        # ── Phase 2: RFQ ────────────────────────────────────────────────
        # Gate added 2026-04-17: RFQ started returning 404s with
        # "market not found for token X" for seemingly valid token IDs,
        # burning circuit-breaker budget (3 consecutive errors → 180s
        # trip). Set FAK_LADDER_ENABLE_RFQ=false in CI deploy vars to
        # skip Phase 2 entirely until the token lookup path is debugged.
        if self._enable_rfq:
            rfq_result = await self._try_rfq(
                token_id,
                side,
                stake_usd,
                entry_cap,
                price_floor,
                start,
            )
            if rfq_result is not None:
                return rfq_result
            # Post-RFQ timeout check — only relevant when RFQ bailed out
            # with no fill. Skip GTC entirely if we're already over budget.
            if _elapsed() > self._max_ladder_elapsed_s:
                return _timeout_result(_elapsed())
        else:
            logger.info(
                "fak_ladder.rfq_skipped",
                extra={
                    "reason": "FAK_LADDER_ENABLE_RFQ=false",
                    "token_id": token_id[:20],
                },
            )

        # ── Phase 3: GTC ────────────────────────────────────────────────
        # Disabled by default after 2026-04-17 phantom-fill incident
        # (see Hub note 64 / audit-task #218). The GTC path can return a
        # success=True ExecutionResult with no real on-chain fill ("gtc_resting"),
        # which propagates as an engine_optimistic phantom trade. Cost overnight:
        # 20/20 phantom + -$77.90 booked vs zero on-chain match.
        if not self._enable_gtc_fallback:
            logger.info(
                "fak_ladder.gtc_skipped",
                extra={
                    "reason": "FAK_LADDER_ENABLE_GTC not set",
                    "fak_attempts": len(fak_prices),
                    "fak_prices": fak_prices,
                },
            )
            return ExecutionResult(
                success=False,
                failure_reason="fak_rfq_exhausted; gtc_fallback_disabled",
                stake_usd=stake_usd,
                execution_mode="none",
                fak_attempts=len(fak_prices),
                fak_prices=fak_prices,
                token_id=token_id,
                execution_start=start,
                execution_end=time.time(),
            )

        gtc_result = await self._try_gtc(
            token_id,
            side,
            stake_usd,
            entry_cap,
            start,
            fak_prices,
            gtc_cap=gtc_cap,
            strategy_id=strategy_id,
        )
        return gtc_result

    async def _try_rfq(
        self,
        token_id: str,
        side: str,
        stake_usd: float,
        entry_cap: float,
        price_floor: float,
        start: float,
    ) -> Optional[ExecutionResult]:
        """Attempt RFQ fill. Returns None if no fill."""
        try:
            shares = math.floor(stake_usd / entry_cap * 100) / 100
            rfq_id, rfq_price = await self._poly.place_rfq_order(
                token_id=token_id,
                direction=side,
                price=entry_cap,
                size=shares,
                max_price=entry_cap,
            )
            if rfq_id and rfq_price:
                fee = self._calc_fee(rfq_price, stake_usd)
                actual_shares = stake_usd / rfq_price if rfq_price > 0 else 0
                return ExecutionResult(
                    success=True,
                    order_id=str(rfq_id),
                    fill_price=rfq_price,
                    fill_size=actual_shares,
                    stake_usd=stake_usd,
                    fee_usd=fee,
                    execution_mode="rfq",
                    fak_attempts=2,
                    fak_prices=[],
                    token_id=token_id,
                    execution_start=start,
                    execution_end=time.time(),
                )
        except Exception as exc:
            logger.warning(
                "fak_ladder.rfq_error",
                extra={"error": str(exc)[:200]},
            )
        return None

    def get_expired_token_ids(self, now: Optional[float] = None) -> list[str]:
        """Return token_ids whose GTC windows have closed (including grace period).

        Called by the periodic heartbeat cancel loop. Uses the ``close_ts``
        stored at placement time plus ``GTC_EXPIRY_GRACE_SECONDS`` to decide
        whether a resting GTC is stale. Safe to call at any cadence.

        Note: dedup keys are (strategy_id, token_id, side) since 2026-05-14
        per-strategy refactor. We project to token_ids here because the
        legacy cancel_expired_gtc_orders(expired_token_ids) signature is
        token-keyed; the cancel function iterates _active_gtc.items()
        internally to find the matching (strategy_id, side) tuples.
        """
        if now is None:
            now = time.time()
        expired: set[str] = set()
        for key, entry in self._active_gtc.items():
            # key is (strategy_id, token_id, side); token_id is index 1.
            token_id = key[1] if len(key) == 3 else key[0]
            if now > entry["close_ts"] + GTC_EXPIRY_GRACE_SECONDS:
                expired.add(token_id)
        return list(expired)

    async def cancel_expired_gtc_orders(self, expired_token_ids: list[str]) -> None:
        """Cancel resting GTC orders for windows that have closed.

        Call this from the engine heartbeat / window resolver after a window
        closes without a fill. Passes the list of token_ids whose windows
        have expired. Any active GTC order for those tokens (across ALL
        strategies and sides) is cancelled via the CLOB API, preventing
        phantom fills on resolved markets.

        Logs ``gtc_window_expired_cancel`` for each cancelled order so
        operators can spot them in the engine log.

        No-op when GTC fallback is disabled or cancel_order is not available
        on the poly client.
        """
        if not expired_token_ids:
            return
        cancel_fn = getattr(self._poly, "cancel_order", None)
        if cancel_fn is None:
            return
        expired_set = set(expired_token_ids)
        # Snapshot keys to mutate dict during iteration.
        keys_to_check = [k for k in self._active_gtc.keys() if (k[1] if len(k) == 3 else k[0]) in expired_set]
        for key in keys_to_check:
            entry = self._active_gtc.get(key)
            if not entry:
                continue
            if len(key) == 3:
                strategy_id, token_id, side = key
            else:
                strategy_id, token_id, side = "", key[0], key[1]
            order_id = entry["order_id"]
            try:
                ok = await cancel_fn(order_id)
                logger.info(
                    "fak_ladder.gtc_window_expired_cancel",
                    extra={
                        "strategy_id": strategy_id,
                        "token_id": token_id[:20],
                        "side": side,
                        "order_id": order_id[:20],
                        "cancelled": ok,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "fak_ladder.gtc_cancel_error",
                    extra={"order_id": order_id[:20], "error": str(exc)[:200]},
                )
            finally:
                # Always remove from dedup registry after expiry attempt —
                # whether the cancel succeeded or not, the window is closed.
                self._active_gtc.pop(key, None)
        # Bound dict size: prune if somehow grown beyond safe limit.
        if len(self._active_gtc) > 1000:
            # Remove oldest half — in FIFO order.
            excess = list(self._active_gtc.keys())[:500]
            for k in excess:
                self._active_gtc.pop(k, None)

    async def _try_gtc(
        self,
        token_id: str,
        side: str,
        stake_usd: float,
        entry_cap: float,
        start: float,
        fak_prices: list[float],
        gtc_cap: Optional[float] = None,
        strategy_id: str = "",
    ) -> ExecutionResult:
        """Place GTC at cap + pi bonus, poll for fill.

        Enforces a 1-GTC-per-(strategy_id, token_id, side) dedup lock so a
        second call from the SAME strategy (e.g. a retry at a different
        eval_offset within the same window) cannot place a duplicate
        resting order. Different strategies on the same (token_id, side)
        are NOT blocked — each strategy owns its independent position.

        ``strategy_id`` defaults to "" for legacy callers (tests); in
        production execute_trade always supplies decision.strategy_id.
        """
        # ── GTC dedup check ──────────────────────────────────────────────
        dedup_key = (strategy_id, token_id, side)
        existing_entry = self._active_gtc.get(dedup_key)
        if existing_entry:
            existing_order_id = existing_entry["order_id"]
            logger.info(
                "fak_ladder.gtc_dedup_blocked",
                extra={
                    "token_id": token_id[:20],
                    "side": side,
                    "existing_order_id": existing_order_id[:20],
                },
            )
            return ExecutionResult(
                success=False,
                failure_reason=(
                    f"gtc_dedup_blocked: order {existing_order_id[:20]} already resting"
                ),
                stake_usd=stake_usd,
                execution_mode="none",
                fak_attempts=len(fak_prices),
                fak_prices=fak_prices,
                token_id=token_id,
                execution_start=start,
                execution_end=time.time(),
            )

        gtc_price = round(gtc_cap if gtc_cap is not None else (entry_cap + self._pi_bonus), 2)
        market_slug = ""  # Not needed for CLOB submission

        try:
            order_id = await self._poly.place_order(
                market_slug=market_slug,
                direction=side,
                price=Decimal(str(gtc_price)),
                stake_usd=stake_usd,
                token_id=token_id,
            )
        except Exception as exc:
            logger.error(
                "fak_ladder.gtc_submit_error",
                extra={"error": str(exc)[:200]},
            )
            return ExecutionResult(
                success=False,
                failure_reason=f"gtc_submit_error: {str(exc)[:200]}",
                stake_usd=stake_usd,
                execution_mode="gtc",
                fak_attempts=2,
                fak_prices=fak_prices,
                token_id=token_id,
                execution_start=start,
                execution_end=time.time(),
            )

        # Register in dedup registry immediately after successful placement.
        # This prevents a second execute_order call (from a retry at a later
        # eval_offset within the same window) from placing a duplicate GTC.
        # close_ts: round placed_at up to the next 300s boundary (current window
        # close). The periodic cancel loop uses close_ts + GTC_EXPIRY_GRACE_SECONDS
        # to detect stale GTCs without needing the caller to supply window_ts.
        order_id_str = str(order_id) if order_id else None
        if order_id_str:
            _placed_at = time.time()
            _close_ts = (
                (_placed_at // _WINDOW_DURATION_SECONDS + 1) * _WINDOW_DURATION_SECONDS
            )
            self._active_gtc[dedup_key] = _GTCEntry(
                order_id=order_id_str,
                placed_at=_placed_at,
                close_ts=_close_ts,
            )
            logger.info(
                "fak_ladder.gtc_registered",
                extra={
                    "token_id": token_id[:20],
                    "side": side,
                    "order_id": order_id_str[:20],
                    "close_ts": int(_close_ts),
                    "active_gtc_count": len(self._active_gtc),
                },
            )

        # Poll briefly for an immediate fill. If the order remains live on the
        # book, treat that as a successful placement so callers record/dedup the
        # order instead of resubmitting duplicate GTC orders for the same window.
        filled = False
        fill_size = 0.0
        elapsed = 0

        while elapsed < self._gtc_max_wait:
            await asyncio.sleep(self._gtc_poll_interval)
            elapsed += self._gtc_poll_interval

            try:
                status = await self._poly.get_order_status(order_id)
                size_matched = float(status.get("size_matched", 0) or 0)
                clob_status = status.get("status", "UNKNOWN")

                if size_matched > 0:
                    filled = True
                    fill_size = size_matched
                    break
                if clob_status not in ("LIVE", "UNKNOWN"):
                    break
            except Exception as exc:
                logger.warning(
                    "fak_ladder.gtc_poll_error",
                    extra={"error": str(exc)[:100], "elapsed": elapsed},
                )

        order_live_on_book = bool(order_id_str) and not filled
        fee = self._calc_fee(gtc_price, stake_usd) if filled else 0.0
        fill_price = (
            round(stake_usd / fill_size, 4) if filled and fill_size > 0 else None
        )

        if filled:
            # Order filled during poll — remove from dedup registry (no longer resting).
            self._active_gtc.pop(dedup_key, None)

        if order_live_on_book:
            return ExecutionResult(
                success=True,
                order_id=order_id_str,
                fill_price=None,
                fill_size=None,
                stake_usd=stake_usd,
                fee_usd=0.0,
                execution_mode="gtc_resting",
                fak_attempts=2,
                fak_prices=fak_prices,
                token_id=token_id,
                execution_start=start,
                execution_end=time.time(),
            )

        # Not filled + not live (rejected/cancelled) — remove from dedup.
        self._active_gtc.pop(dedup_key, None)
        return ExecutionResult(
            success=filled,
            order_id=order_id_str,
            fill_price=fill_price,
            fill_size=fill_size if filled else None,
            stake_usd=stake_usd,
            fee_usd=fee,
            execution_mode="gtc",
            fak_attempts=2,
            fak_prices=fak_prices,
            failure_reason=None if filled else "gtc_unfilled",
            token_id=token_id,
            execution_start=start,
            execution_end=time.time(),
        )

    @staticmethod
    def _calc_fee(price: float, stake: float) -> float:
        """Polymarket binary options fee: 7.2% * p * (1-p) * stake."""
        return FEE_MULTIPLIER * price * (1.0 - price) * stake
