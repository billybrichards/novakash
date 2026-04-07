"""
Price Ladder — v9.0

Configurable order execution ladder for Polymarket CLOB.
Supports FAK (Fill-And-Kill), FOK (Fill-Or-Kill), and GTC order types.

v9.0 default: FAK — fills what exists at ≤ limit price, cancels remainder.
With dynamic caps ($0.55/$0.65), FAK is safe from terrible fills.

Flow:
  1. Query live CLOB book for best ask
  2. Submit FAK/FOK at cap + π cents (if within threshold)
  3. If killed/partial, wait interval, refresh book, bump, retry
  4. Return structured result with fill price, shares matched, attempts

Order type configurable via ORDER_TYPE env var (FAK/FOK/GTC).
GTC fallback in strategy if ladder exhausts all attempts.

CRITICAL: All CLOB operations go through PolymarketClient — NO direct HTTP calls here.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from execution.polymarket_client import PolymarketClient

logger = structlog.get_logger(__name__)


# ── Config defaults (overridable via env) ────────────────────────────────────

def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


FOK_ATTEMPTS_DEFAULT = 5
FOK_INTERVAL_S_DEFAULT = 2.0
FOK_BUMP_DEFAULT = 0.01
FOK_PI_BONUS_CENTS_DEFAULT = 0.0314  # π cents
FOK_PI_PERCENT_THRESHOLD_DEFAULT = 3.14  # π%
ORDER_TYPE_DEFAULT = "FAK"  # v9.0 default: Fill-And-Kill


@dataclass
class FOKResult:
    """Result from a price ladder execution (FAK/FOK)."""
    filled: bool
    fill_price: Optional[float]
    fill_step: Optional[int]        # Which attempt number filled (1-indexed)
    shares: Optional[float]
    attempts: int
    order_id: Optional[str]
    attempted_prices: list[float] = field(default_factory=list)
    abort_reason: Optional[str] = None  # Set when ladder aborts before first attempt
    partial: bool = False  # True if FAK partial fill (size_matched < requested)
    order_type: str = "FAK"  # Which order type was used


class FOKLadder:
    """
    Price execution ladder (v9.0 — supports FAK/FOK/GTC).

    Queries the live CLOB book and attempts to fill at cap + π cents,
    retrying up to FOK_ATTEMPTS times with price bumps.

    Default order type: FAK (Fill-And-Kill) — fills partial liquidity safely.
    Configurable via ORDER_TYPE env var.

    All CLOB operations delegate to PolymarketClient — no direct HTTP calls.
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
        Execute price ladder for a BUY order.

        Args:
            token_id: CLOB outcome token ID.
            direction: "BUY" (always BUY — we buy YES or NO tokens).
            stake_usd: Notional USD to spend.
            max_price: Hard cap (dynamic per v9.0 tier).
            min_price: Hard floor — abort if best_ask < min_price.

        Returns:
            FOKResult with fill details or filled=False on miss/abort.
        """
        # Re-read config each call (hot-reload from env)
        max_attempts = _env_int("FOK_ATTEMPTS", FOK_ATTEMPTS_DEFAULT)
        interval_s = _env_float("FOK_INTERVAL_S", FOK_INTERVAL_S_DEFAULT)
        bump = _env_float("FOK_BUMP", FOK_BUMP_DEFAULT)
        pi_bonus_cents = _env_float("FOK_PI_BONUS_CENTS", FOK_PI_BONUS_CENTS_DEFAULT)
        pi_percent_threshold = _env_float("FOK_PI_PERCENT_THRESHOLD", FOK_PI_PERCENT_THRESHOLD_DEFAULT)
        order_type = os.environ.get("ORDER_TYPE", ORDER_TYPE_DEFAULT).upper()

        # Pi bonus: allow up to cap+π cents
        # Threshold and max must be the same — if we'd pay cap+π, check against cap+π
        effective_max_price = max_price + pi_bonus_cents
        pi_threshold_price = effective_max_price  # unified: check and limit are identical

        attempted_prices: list[float] = []

        # ── Step 1: Get initial best ask ──────────────────────────────────
        try:
            best_ask = await self._poly.get_clob_best_ask(token_id)
        except Exception as exc:
            self._log.warning("price_ladder.book_error", error=str(exc)[:200])
            return FOKResult(
                filled=False, fill_price=None, fill_step=None, shares=None,
                attempts=0, order_id=None, attempted_prices=[],
                abort_reason=f"book_error: {str(exc)[:100]}",
                order_type=order_type,
            )

        # ── Step 2: Price bounds check ────────────────────────────────────
        if best_ask < min_price:
            self._log.warning("price_ladder.abort_floor",
                best_ask=f"${best_ask:.4f}", floor=f"${min_price:.4f}")
            return FOKResult(
                filled=False, fill_price=None, fill_step=None, shares=None,
                attempts=0, order_id=None, attempted_prices=[],
                abort_reason=f"best_ask ${best_ask:.4f} < floor ${min_price:.4f}",
                order_type=order_type,
            )

        # ── Step 2b: Pi bonus check (now AFTER best_ask is known) ────────
        within_pi = best_ask <= pi_threshold_price
        if within_pi:
            current_price = min(best_ask, effective_max_price)
        else:
            current_price = max_price

        self._log.info(
            "price_ladder.start",
            order_type=order_type,
            token_id=token_id[:20] + "...",
            stake_usd=f"${stake_usd:.2f}",
            base_cap=f"${max_price:.4f}",
            effective_cap=f"${effective_max_price:.4f}",
            best_ask=f"${best_ask:.4f}",
            within_pi=within_pi,
            starting_price=f"${current_price:.4f}",
            max_attempts=max_attempts,
        )

        # ── Steps 3–6: Attempt loop (FAK/FOK) ─────────────────────────────
        import math

        for attempt in range(1, max_attempts + 1):
            attempt_price = min(round(current_price, 4), effective_max_price)
            attempt_price = round(attempt_price, 2)  # CLOB requires 2dp
            attempted_prices.append(attempt_price)

            # Size calculation: floor to 2dp, adjust until maker_amount is clean
            size = math.floor(stake_usd / attempt_price * 100) / 100
            for _adj in range(100):
                _maker = round(attempt_price * size, 6)
                if abs(_maker - round(_maker, 2)) < 1e-9:
                    break
                size -= 0.01
            size = max(size, 0.01)

            self._log.info(
                "price_ladder.attempt",
                attempt=attempt,
                order_type=order_type,
                price=f"${attempt_price:.4f}",
                size=f"{size:.2f}",
            )

            # ── Submit order (FAK or FOK via configurable type) ──────────
            try:
                result = await self._poly.place_market_order(
                    token_id=token_id,
                    price=attempt_price,
                    size=size,
                    order_type=order_type,
                )
            except Exception as exc:
                self._log.warning("price_ladder.order_error",
                    attempt=attempt, error=str(exc)[:200])
                if attempt < max_attempts:
                    await asyncio.sleep(interval_s)
                    try:
                        current_price = await self._poly.get_clob_best_ask(token_id)
                        current_price = min(current_price + bump, effective_max_price)
                    except Exception:
                        current_price = min(attempt_price + bump, effective_max_price)
                    continue
                else:
                    break

            filled = result.get("filled", False)
            size_matched = float(result.get("size_matched", 0.0) or 0.0)
            order_id = result.get("order_id")

            self._log.info(
                "price_ladder.result",
                attempt=attempt,
                order_type=order_type,
                price=f"${attempt_price:.4f}",
                filled=filled,
                size_matched=f"{size_matched:.2f}",
                requested=f"{size:.2f}",
                order_id=str(order_id)[:20] if order_id else "none",
            )

            # ── Check fill (FAK may partial-fill) ────────────────────────
            if size_matched > 0:
                actual_shares = size_matched
                actual_fill_price = round(stake_usd / actual_shares, 4) if actual_shares > 0 else attempt_price
                is_partial = actual_shares < (size * 0.95)  # >5% shortfall = partial

                self._log.info(
                    "price_ladder.filled",
                    attempt=attempt,
                    order_type=order_type,
                    fill_price=f"${actual_fill_price:.4f}",
                    shares=f"{actual_shares:.2f}",
                    partial=is_partial,
                )

                return FOKResult(
                    filled=True,
                    fill_price=actual_fill_price,
                    fill_step=attempt,
                    shares=actual_shares,
                    attempts=attempt,
                    order_id=str(order_id) if order_id else None,
                    attempted_prices=attempted_prices,
                    partial=is_partial,
                    order_type=order_type,
                )

            # ── Killed / zero fill — prepare next attempt ────────────────
            if attempt < max_attempts:
                self._log.info("price_ladder.retry",
                    attempt=attempt, wait_s=interval_s)
                await asyncio.sleep(interval_s)
                try:
                    fresh_ask = await self._poly.get_clob_best_ask(token_id)
                    current_price = min(fresh_ask + bump, effective_max_price)
                except Exception:
                    current_price = min(attempt_price + bump, effective_max_price)

        # ── All attempts exhausted ───────────────────────────────────────
        self._log.warning("price_ladder.exhausted",
            order_type=order_type,
            attempts=len(attempted_prices),
            prices=[f"${p:.4f}" for p in attempted_prices])

        return FOKResult(
            filled=False, fill_price=None, fill_step=None, shares=None,
            attempts=len(attempted_prices), order_id=None,
            attempted_prices=attempted_prices,
            order_type=order_type,
        )
