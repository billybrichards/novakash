"""
Price Ladder — v9.0

Simple two-shot FAK execution for Polymarket CLOB.

Flow:
  1. FAK at cap — fill whatever exists at ≤ cap price
  2. If zero fill → FAK at cap + π cents (3.14¢) — one more try
  3. If still zero → return exhausted (strategy falls back to GTC)

Order type configurable via ORDER_TYPE env var (FAK/FOK).
With dynamic caps ($0.55/$0.65), FAK is safe from terrible fills.

CRITICAL: All CLOB operations go through PolymarketClient — NO direct HTTP calls here.
"""

from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from execution.polymarket_client import PolymarketClient

logger = structlog.get_logger(__name__)


# ── Config defaults ──────────────────────────────────────────────────────────

PI_BONUS_CENTS = 0.0314  # π cents
RETRY_WAIT_S = 2.0


@dataclass
class FOKResult:
    """Result from a price ladder execution (FAK/FOK)."""
    filled: bool
    fill_price: Optional[float]
    fill_step: Optional[int]        # Which attempt filled (1=cap, 2=cap+π)
    shares: Optional[float]
    attempts: int
    order_id: Optional[str]
    attempted_prices: list[float] = field(default_factory=list)
    abort_reason: Optional[str] = None
    partial: bool = False           # True if FAK partial fill
    order_type: str = "FAK"


class FOKLadder:
    """
    Two-shot price ladder (v9.0).

    Attempt 1: FAK at cap (e.g. $0.65)
    Attempt 2: FAK at cap + π cents ($0.6814) if first attempt got zero fill
    Then done — strategy handles GTC fallback.
    """

    def __init__(self, poly_client: "PolymarketClient") -> None:
        self._poly = poly_client
        self._log = logger.bind(component="price_ladder")

    async def execute(
        self,
        token_id: str,
        direction: str,
        stake_usd: float,
        max_price: float = 0.65,
        min_price: float = 0.30,
    ) -> FOKResult:
        """
        Execute two-shot FAK/FOK ladder.

        Attempt 1: FAK at max_price (cap)
        Attempt 2: FAK at max_price + π cents (if attempt 1 got zero fill)

        Returns FOKResult with fill details or filled=False.
        """
        order_type = os.environ.get("ORDER_TYPE", "FAK").upper()
        pi_bonus = float(os.environ.get("FOK_PI_BONUS_CENTS", str(PI_BONUS_CENTS)))
        wait_s = float(os.environ.get("FOK_INTERVAL_S", str(RETRY_WAIT_S)))

        cap = round(max_price, 2)
        cap_plus_pi = round(max_price + pi_bonus, 2)
        attempted_prices: list[float] = []

        # ── Step 1: Check CLOB book ─────────────────────────────────────
        try:
            best_ask = await self._poly.get_clob_best_ask(token_id)
        except Exception as exc:
            self._log.warning("price_ladder.book_error", error=str(exc)[:200])
            return FOKResult(
                filled=False, fill_price=None, fill_step=None, shares=None,
                attempts=0, order_id=None, abort_reason=f"book_error: {str(exc)[:100]}",
                order_type=order_type,
            )

        if best_ask < min_price:
            self._log.warning("price_ladder.abort_floor",
                best_ask=f"${best_ask:.4f}", floor=f"${min_price:.4f}")
            return FOKResult(
                filled=False, fill_price=None, fill_step=None, shares=None,
                attempts=0, order_id=None,
                abort_reason=f"best_ask ${best_ask:.4f} < floor ${min_price:.4f}",
                order_type=order_type,
            )

        self._log.info("price_ladder.start",
            order_type=order_type, best_ask=f"${best_ask:.4f}",
            cap=f"${cap:.2f}", cap_pi=f"${cap_plus_pi:.2f}",
            stake=f"${stake_usd:.2f}")

        # ── Attempt 1: FAK at cap ───────────────────────────────────────
        result_1 = await self._submit(token_id, cap, stake_usd, order_type, attempt=1)
        attempted_prices.append(cap)

        if result_1 and result_1.get("size_matched", 0) > 0:
            return self._build_result(result_1, cap, stake_usd, 1, attempted_prices, order_type)

        # ── Wait, then Attempt 2: FAK at cap + π ────────────────────────
        self._log.info("price_ladder.retry_with_pi",
            cap=f"${cap:.2f}", cap_pi=f"${cap_plus_pi:.2f}", wait_s=wait_s)
        await asyncio.sleep(wait_s)

        result_2 = await self._submit(token_id, cap_plus_pi, stake_usd, order_type, attempt=2)
        attempted_prices.append(cap_plus_pi)

        if result_2 and result_2.get("size_matched", 0) > 0:
            return self._build_result(result_2, cap_plus_pi, stake_usd, 2, attempted_prices, order_type)

        # ── Both attempts failed ────────────────────────────────────────
        self._log.warning("price_ladder.exhausted",
            order_type=order_type, prices=[f"${p:.2f}" for p in attempted_prices])

        return FOKResult(
            filled=False, fill_price=None, fill_step=None, shares=None,
            attempts=2, order_id=None, attempted_prices=attempted_prices,
            order_type=order_type,
        )

    async def _submit(
        self, token_id: str, price: float, stake_usd: float,
        order_type: str, attempt: int,
    ) -> Optional[dict]:
        """Submit a single FAK/FOK order and return raw result."""
        size = self._calc_size(price, stake_usd)
        if size <= 0:
            return None

        self._log.info("price_ladder.attempt",
            attempt=attempt, order_type=order_type,
            price=f"${price:.2f}", size=f"{size:.2f}")

        try:
            result = await self._poly.place_market_order(
                token_id=token_id,
                price=price,
                size=size,
                order_type=order_type,
            )
        except Exception as exc:
            err_str = str(exc)
            # FAK "no orders found to match" is normal — not an error
            if "no orders found to match" in err_str:
                self._log.info("price_ladder.no_match",
                    attempt=attempt, order_type=order_type,
                    price=f"${price:.2f}", note="no sellers at this price")
                return {"size_matched": 0, "order_id": None, "filled": False}
            # "invalid amounts" = precision issue, treat as zero fill
            if "invalid amounts" in err_str:
                self._log.info("price_ladder.precision_error",
                    attempt=attempt, price=f"${price:.2f}", size=f"{size:.2f}",
                    note="maker_amount precision rejected by CLOB")
                return {"size_matched": 0, "order_id": None, "filled": False}
            self._log.warning("price_ladder.order_error",
                attempt=attempt, error=err_str[:200])
            return None

        size_matched = float(result.get("size_matched", 0) or 0)
        order_id = result.get("order_id")

        self._log.info("price_ladder.result",
            attempt=attempt, order_type=order_type,
            price=f"${price:.2f}", size_matched=f"{size_matched:.2f}",
            requested=f"{size:.2f}", order_id=str(order_id)[:20] if order_id else "none")

        return result

    def _build_result(
        self, result: dict, price: float, stake_usd: float,
        attempt: int, attempted_prices: list, order_type: str,
    ) -> FOKResult:
        """Build FOKResult from a successful fill."""
        size_matched = float(result.get("size_matched", 0) or 0)
        order_id = result.get("order_id")
        fill_price = round(stake_usd / size_matched, 4) if size_matched > 0 else price
        requested = self._calc_size(price, stake_usd)
        is_partial = size_matched < (requested * 0.95)

        self._log.info("price_ladder.filled",
            attempt=attempt, order_type=order_type,
            fill_price=f"${fill_price:.4f}", shares=f"{size_matched:.2f}",
            partial=is_partial)

        return FOKResult(
            filled=True, fill_price=fill_price, fill_step=attempt,
            shares=size_matched, attempts=attempt,
            order_id=str(order_id) if order_id else None,
            attempted_prices=attempted_prices,
            partial=is_partial, order_type=order_type,
        )

    # Polymarket minimum order size (shares). Orders below this are rejected with 400.
    POLY_MIN_SHARES: float = 5.0

    @staticmethod
    def _calc_size(price: float, stake_usd: float) -> float:
        """Calculate CLOB-compliant size (2dp price, clean maker_amount ≤2dp).

        Enforces Polymarket minimum of 5 shares. If calculated size < 5,
        bumps up to 5 (the stake will be slightly higher than requested).
        """
        _price = round(price, 2)  # CLOB enforces 2dp on FAK/FOK prices
        size = math.floor(stake_usd / _price * 100) / 100
        # Ensure maker_amount (price × size) is clean to 2dp
        for _ in range(100):
            _maker = round(_price * size, 6)
            if abs(_maker - round(_maker, 2)) < 1e-9:
                break
            size -= 0.01
        # Enforce Polymarket minimum order size
        size = max(size, FOKPriceLadder.POLY_MIN_SHARES)
        return size
