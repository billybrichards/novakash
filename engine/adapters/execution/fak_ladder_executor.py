"""FAK Ladder Executor -- live execution via FAK ladder -> RFQ -> GTC fallback.

Wraps the existing FOKLadder + PolymarketClient.place_rfq_order +
PolymarketClient.place_order into a single execute_order() call that
satisfies the OrderExecutionPort interface.

This adapter owns the multi-step execution strategy. The use case just
calls execute_order() and gets back an ExecutionResult.

Audit: SP-06 Phase 4.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from decimal import Decimal
from typing import Any, Optional

from use_cases.ports.execution import OrderExecutionPort
from domain.ports import PolymarketClientPort
from domain.value_objects import ExecutionResult

logger = logging.getLogger(__name__)

# Polymarket binary options fee
FEE_MULTIPLIER = 0.072

# GTC poll config
DEFAULT_GTC_POLL_INTERVAL = 5
DEFAULT_GTC_MAX_WAIT = 60

# Pi bonus for GTC after FAK exhaustion
DEFAULT_PI_BONUS = 0.0314

# Phase-3 GTC fallback default. Disabled (False) post-2026-04-17 incident:
# 20/20 overnight gtc_resting orders booked as RESOLVED_LOSS in trades table
# but never matched a poly_fills row on-chain (engine_optimistic state) —
# -$77.90 phantom loss. See Hub note 64, audit-task #218.
# Set FAK_LADDER_ENABLE_GTC=true (case-insensitive 1/true/yes) to re-enable
# the legacy Phase-3 fallback for back-compat / experimentation.
_DEFAULT_ENABLE_GTC_FALLBACK = False


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
    ) -> None:
        self._poly = poly_client
        self._pi_bonus = pi_bonus_cents
        self._gtc_poll_interval = gtc_poll_interval
        self._gtc_max_wait = gtc_max_wait
        if enable_gtc_fallback is None:
            env = os.environ.get("FAK_LADDER_ENABLE_GTC", "").strip().lower()
            self._enable_gtc_fallback = env in ("1", "true", "yes", "on")
        else:
            self._enable_gtc_fallback = bool(enable_gtc_fallback)
        if not self._enable_gtc_fallback:
            logger.info(
                "fak_ladder.init",
                extra={"gtc_fallback": "disabled (default; set FAK_LADDER_ENABLE_GTC=true to re-enable)"},
            )

    async def execute_order(
        self,
        token_id: str,
        side: str,
        stake_usd: float,
        entry_cap: float,
        price_floor: float,
    ) -> ExecutionResult:
        """Execute using the FAK -> RFQ -> GTC ladder.

        Returns ExecutionResult. Does not raise.
        """
        start = time.time()
        fak_prices: list[float] = []

        # ── Phase 1: FAK ladder ─────────────────────────────────────────
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

        # ── Phase 2: RFQ ────────────────────────────────────────────────
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

    async def _try_gtc(
        self,
        token_id: str,
        side: str,
        stake_usd: float,
        entry_cap: float,
        start: float,
        fak_prices: list[float],
    ) -> ExecutionResult:
        """Place GTC at cap + pi bonus, poll for fill."""
        gtc_price = round(entry_cap + self._pi_bonus, 2)
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

        order_id_str = str(order_id) if order_id else None
        order_live_on_book = bool(order_id_str) and not filled
        fee = self._calc_fee(gtc_price, stake_usd) if filled else 0.0
        fill_price = (
            round(stake_usd / fill_size, 4) if filled and fill_size > 0 else None
        )

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
