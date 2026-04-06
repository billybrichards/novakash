"""
FOK Execution Ladder — v8.0 Phase 2

Fill-or-Kill order ladder for Polymarket CLOB.

Instead of a single GTC/GTD order at a stale Gamma price, the FOK ladder:
  1. Queries the live CLOB book for the best ask
  2. Submits a FOK at that price
  3. If killed (not immediately filled), waits FOK_INTERVAL_S, refreshes the book,
     bumps price by FOK_BUMP, and retries — up to FOK_ATTEMPTS times
  4. Returns a structured result with fill price, step, and attempts used

This replaces the single GTC placement in five_min_vpin.py when FOK_ENABLED=true.
The existing GTC path is preserved as a fallback.

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


@dataclass
class FOKResult:
    """Result from a FOK ladder execution."""
    filled: bool
    fill_price: Optional[float]
    fill_step: Optional[int]        # Which attempt number filled (1-indexed)
    shares: Optional[float]
    attempts: int
    order_id: Optional[str]
    attempted_prices: list[float] = field(default_factory=list)
    abort_reason: Optional[str] = None  # Set when ladder aborts before first attempt


class FOKLadder:
    """
    Fill-or-Kill execution ladder.

    Queries the live CLOB book and attempts to fill at progressively higher
    prices (up to FOK_ATTEMPTS times), bumping by FOK_BUMP each miss.

    All CLOB operations delegate to PolymarketClient — no direct HTTP calls.

    Args:
        poly_client: Initialised PolymarketClient instance.
    """

    def __init__(self, poly_client: "PolymarketClient") -> None:
        self._poly = poly_client
        self._log = logger.bind(component="fok_ladder")

        # Config: read from env each time execute() is called so hot-reload works
        # (but store here for logging convenience)
        self._max_attempts = _env_int("FOK_ATTEMPTS", FOK_ATTEMPTS_DEFAULT)
        self._interval_s = _env_float("FOK_INTERVAL_S", FOK_INTERVAL_S_DEFAULT)
        self._bump = _env_float("FOK_BUMP", FOK_BUMP_DEFAULT)

    async def execute(
        self,
        token_id: str,
        direction: str,
        stake_usd: float,
        max_price: float = 0.73,
        min_price: float = 0.30,
    ) -> FOKResult:
        """
        Execute FOK ladder for a BUY order.

        Args:
            token_id: CLOB outcome token ID.
            direction: "BUY" (always BUY — we buy YES or NO tokens).
            stake_usd: Notional USD to spend.
            max_price: Hard cap — abort if best_ask > max_price.
            min_price: Hard floor — abort if best_ask < min_price.

        Returns:
            FOKResult with fill details or filled=False on miss/abort.
        """
        # Re-read config each call (hot-reload from env)
        max_attempts = _env_int("FOK_ATTEMPTS", FOK_ATTEMPTS_DEFAULT)
        interval_s = _env_float("FOK_INTERVAL_S", FOK_INTERVAL_S_DEFAULT)
        bump = _env_float("FOK_BUMP", FOK_BUMP_DEFAULT)

        self._log.info(
            "fok_ladder.start",
            token_id=token_id[:20] + "..." if len(token_id) > 20 else token_id,
            stake_usd=f"${stake_usd:.2f}",
            max_price=f"${max_price:.4f}",
            min_price=f"${min_price:.4f}",
            max_attempts=max_attempts,
            interval_s=interval_s,
            bump=f"${bump:.4f}",
        )

        attempted_prices: list[float] = []

        # ── Step 1: Get initial best ask ──────────────────────────────────
        try:
            best_ask = await self._poly.get_clob_best_ask(token_id)
        except Exception as exc:
            self._log.warning("fok_ladder.book_error_initial", error=str(exc)[:200])
            return FOKResult(
                filled=False,
                fill_price=None,
                fill_step=None,
                shares=None,
                attempts=0,
                order_id=None,
                attempted_prices=[],
                abort_reason=f"book_error: {str(exc)[:100]}",
            )

        # ── Step 2: Price bounds check ────────────────────────────────────
        if best_ask < min_price:
            self._log.warning(
                "fok_ladder.abort_floor",
                best_ask=f"${best_ask:.4f}",
                floor=f"${min_price:.4f}",
            )
            return FOKResult(
                filled=False,
                fill_price=None,
                fill_step=None,
                shares=None,
                attempts=0,
                order_id=None,
                attempted_prices=[],
                abort_reason=f"best_ask ${best_ask:.4f} < floor ${min_price:.4f}",
            )

        if best_ask > max_price:
            self._log.warning(
                "fok_ladder.abort_cap",
                best_ask=f"${best_ask:.4f}",
                cap=f"${max_price:.4f}",
            )
            return FOKResult(
                filled=False,
                fill_price=None,
                fill_step=None,
                shares=None,
                attempts=0,
                order_id=None,
                attempted_prices=[],
                abort_reason=f"best_ask ${best_ask:.4f} > cap ${max_price:.4f}",
            )

        current_price = best_ask

        # ── Steps 3–6: FOK attempt loop ───────────────────────────────────
        for attempt in range(1, max_attempts + 1):
            # Cap each attempt at max_price
            attempt_price = min(round(current_price, 4), max_price)
            attempted_prices.append(attempt_price)

            # Calculate size for this attempt
            size = round(stake_usd / attempt_price, 2)

            self._log.info(
                "fok_ladder.attempt",
                attempt=attempt,
                max_attempts=max_attempts,
                price=f"${attempt_price:.4f}",
                size=f"{size:.2f}",
                token_id=token_id[:20] + "..." if len(token_id) > 20 else token_id,
            )

            # ── Step 3: Submit FOK ────────────────────────────────────────
            try:
                fok_result = await self._poly.place_fok_order(
                    token_id=token_id,
                    price=attempt_price,
                    size=size,
                )
            except Exception as exc:
                self._log.warning(
                    "fok_ladder.order_error",
                    attempt=attempt,
                    price=f"${attempt_price:.4f}",
                    error=str(exc)[:200],
                )
                # Non-fatal — continue to next attempt if cap not exceeded
                if attempt < max_attempts and attempt_price < max_price:
                    await asyncio.sleep(interval_s)
                    try:
                        current_price = await self._poly.get_clob_best_ask(token_id)
                        current_price = min(current_price + bump, max_price)
                    except Exception:
                        current_price = min(attempt_price + bump, max_price)
                    continue
                else:
                    break

            filled = fok_result.get("filled", False)
            size_matched = fok_result.get("size_matched", 0.0)
            order_id = fok_result.get("order_id")

            self._log.info(
                "fok_ladder.attempt_result",
                attempt=attempt,
                price=f"${attempt_price:.4f}",
                filled=filled,
                size_matched=size_matched,
                order_id=str(order_id)[:20] if order_id else "none",
            )

            # ── Step 4: Check fill ────────────────────────────────────────
            if filled and float(size_matched) > 0:
                actual_shares = float(size_matched)
                actual_fill_price = round(stake_usd / actual_shares, 4) if actual_shares > 0 else attempt_price

                self._log.info(
                    "fok_ladder.filled",
                    attempt=attempt,
                    fill_price=f"${actual_fill_price:.4f}",
                    shares=f"{actual_shares:.4f}",
                    total_attempts=attempt,
                    attempted_prices=attempted_prices,
                )

                return FOKResult(
                    filled=True,
                    fill_price=actual_fill_price,
                    fill_step=attempt,
                    shares=actual_shares,
                    attempts=attempt,
                    order_id=str(order_id) if order_id else None,
                    attempted_prices=attempted_prices,
                )

            # ── Step 5: FOK was killed — prepare next attempt ─────────────
            if attempt < max_attempts:
                self._log.info(
                    "fok_ladder.killed_retry",
                    attempt=attempt,
                    price=f"${attempt_price:.4f}",
                    wait_s=interval_s,
                    next_bump=f"${bump:.4f}",
                )
                await asyncio.sleep(interval_s)

                # ── Step 5 cont: Refresh book and bump ────────────────────
                try:
                    fresh_ask = await self._poly.get_clob_best_ask(token_id)
                    # Use fresh book price as base, then bump
                    current_price = min(fresh_ask + bump, max_price)
                    self._log.info(
                        "fok_ladder.fresh_book",
                        fresh_ask=f"${fresh_ask:.4f}",
                        next_price=f"${current_price:.4f}",
                    )
                except Exception as exc:
                    # Book query failed — bump from last price
                    current_price = min(attempt_price + bump, max_price)
                    self._log.debug(
                        "fok_ladder.fresh_book_failed",
                        fallback_price=f"${current_price:.4f}",
                        error=str(exc)[:100],
                    )

                # Abort early if we've already hit the cap
                if current_price >= max_price and attempt_price >= max_price:
                    self._log.info(
                        "fok_ladder.cap_reached",
                        price=f"${current_price:.4f}",
                        cap=f"${max_price:.4f}",
                    )
                    break

        # ── Step 7: All attempts exhausted ───────────────────────────────
        self._log.warning(
            "fok_ladder.exhausted",
            attempts=len(attempted_prices),
            attempted_prices=[f"${p:.4f}" for p in attempted_prices],
        )

        return FOKResult(
            filled=False,
            fill_price=None,
            fill_step=None,
            shares=None,
            attempts=len(attempted_prices),
            order_id=None,
            attempted_prices=attempted_prices,
        )
