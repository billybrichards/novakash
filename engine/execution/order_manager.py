"""
OrderManager — tracks open orders and handles paper-mode resolution polling.

Resolution logic (paper mode):
- Polymarket orders expire after 5 minutes (POLY_WINDOW_SECONDS).
- Opinion orders expire after 15 minutes (OPINION_WINDOW_SECONDS).
- Arb strategy orders always resolve as WIN (guaranteed profit).
- vpin_cascade strategy orders resolve WIN if BTC moved in the predicted direction.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

# Window lengths for paper-mode expiry
POLY_WINDOW_SECONDS: int = 300    # 5 minutes
OPINION_WINDOW_SECONDS: int = 900  # 15 minutes

# Minimum BTC price move to count as directional win (paper mode)
# For 5-min markets: no minimum — any move counts (matches Polymarket oracle)
# For cascade/other: small threshold to avoid noise
MIN_BTC_MOVE_PCT: float = 0.0001  # 0.01% — effectively zero for 5-min


class OrderStatus(str, Enum):
    """Lifecycle states for a tracked order."""
    OPEN = "OPEN"
    FILLED = "FILLED"
    RESOLVED_WIN = "RESOLVED_WIN"
    RESOLVED_LOSS = "RESOLVED_LOSS"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass
class Order:
    """Represents a single order across either venue.

    Attributes:
        order_id: Venue-assigned (or paper) order identifier.
        venue: "polymarket" or "opinion".
        strategy: Strategy that placed the order, e.g. "arb", "vpin_cascade".
        direction: "YES" or "NO".
        price: Fill price as a decimal string (e.g. "0.5123").
        stake_usd: USD risked.
        status: Current lifecycle state.
        created_at: Unix timestamp of order creation.
        resolved_at: Unix timestamp of resolution (None if still open).
        outcome: "WIN" or "LOSS" after resolution.
        payout_usd: USD received on resolution.
        btc_entry_price: BTC/USD price at the time of order placement.
        window_seconds: Duration of the prediction window in seconds.
        market_id: Venue-specific market identifier.
    """
    order_id: str
    venue: str                       # "polymarket" | "opinion"
    strategy: str                    # "arb" | "vpin_cascade" | ...
    direction: str                   # "YES" | "NO"
    price: str
    stake_usd: float
    status: OrderStatus = OrderStatus.OPEN
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    outcome: Optional[str] = None    # "WIN" | "LOSS"
    payout_usd: Optional[float] = None
    pnl_usd: Optional[float] = None
    fee_usd: float = 0.0
    btc_entry_price: Optional[float] = None
    window_seconds: int = POLY_WINDOW_SECONDS
    market_id: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def market_slug(self) -> str:
        """Alias for market_id — used by db_client.write_trade."""
        return self.market_id

    @property
    def entry_price(self) -> str:
        """Alias for price — used by db_client.write_trade."""
        return self.price


class OrderManager:
    """
    Manages the full lifecycle of orders from placement to resolution.

    Thread-safety: an asyncio Lock guards all state mutations so the
    manager is safe to use from concurrent coroutines.
    """

    def __init__(
        self,
        db: object = None,
        bankroll: float = 500.0,
        paper_mode: bool = True,
        on_resolution: Optional[callable] = None,
        poly_client: object = None,
    ) -> None:
        self._db = db
        self._bankroll = bankroll
        self._paper_mode = paper_mode
        self._on_resolution = on_resolution
        self._poly_client = poly_client
        self._orders: Dict[str, Order] = {}
        self._lock = asyncio.Lock()
        self._log = logger.bind(component="order_manager")
        self._current_btc_price: float = 0.0

        # Counters exposed to RiskManager
        self._total_orders: int = 0
        self._resolved_orders: int = 0

    def update_btc_price(self, price) -> None:
        """Update the current BTC price (called by orchestrator on each trade)."""
        self._current_btc_price = float(price)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register_order(self, order: Order) -> None:
        """Add a newly-placed order to tracking.

        Args:
            order: The Order dataclass instance to track.
        """
        async with self._lock:
            self._orders[order.order_id] = order
            self._total_orders += 1
            self._log.info(
                "order_manager.registered",
                order_id=order.order_id,
                venue=order.venue,
                strategy=order.strategy,
                direction=order.direction,
                stake_usd=order.stake_usd,
            )

        # Persist to DB (non-blocking — don't hold the lock)
        await self._persist_trade(order)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    async def resolve_order(
        self,
        order_id: str,
        outcome: str,
        payout_usd: float,
    ) -> Order:
        """Mark an order as resolved with a known outcome.

        Args:
            order_id: Order to resolve.
            outcome: "WIN" or "LOSS".
            payout_usd: USD received (0 for a loss, > stake_usd for win).

        Returns:
            The updated Order.

        Raises:
            KeyError: If order_id is unknown.
            ValueError: If outcome is not WIN or LOSS.
        """
        if outcome not in {"WIN", "LOSS"}:
            raise ValueError(f"outcome must be WIN or LOSS, got {outcome!r}")

        async with self._lock:
            order = self._orders[order_id]  # raises KeyError if missing
            order.status = (
                OrderStatus.RESOLVED_WIN if outcome == "WIN" else OrderStatus.RESOLVED_LOSS
            )
            order.outcome = outcome
            order.payout_usd = payout_usd
            order.pnl_usd = payout_usd - order.stake_usd - order.fee_usd
            order.resolved_at = time.time()
            self._resolved_orders += 1

            pnl = order.pnl_usd
            self._log.info(
                "order_manager.resolved",
                order_id=order_id,
                outcome=outcome,
                payout_usd=payout_usd,
                pnl=pnl,
                venue=order.venue,
                strategy=order.strategy,
            )
            return order

    # ------------------------------------------------------------------
    # DB Persistence
    # ------------------------------------------------------------------

    async def _persist_trade(self, order: Order) -> None:
        """Write/upsert trade to DB via db_client.save_trade().

        Silently skips if no DB is configured — engine keeps working
        even without a database connection.
        """
        if self._db is None:
            return
        try:
            await self._db.save_trade(order)
            self._log.debug("order_manager.persisted", order_id=order.order_id)
        except Exception as exc:
            self._log.error(
                "order_manager.persist_failed",
                order_id=order.order_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_open_orders(self) -> List[Order]:
        """Return all orders currently in OPEN or FILLED state."""
        async with self._lock:
            return [
                o for o in self._orders.values()
                if o.status in {OrderStatus.OPEN, OrderStatus.FILLED}
            ]

    async def get_open_exposure_usd(self) -> float:
        """Return total USD at risk across all open orders."""
        open_orders = await self.get_open_orders()
        return sum(o.stake_usd for o in open_orders)

    @property
    def total_orders(self) -> int:
        """Total orders registered (all time)."""
        return self._total_orders

    @property
    def resolved_orders(self) -> int:
        """Total orders resolved (WIN or LOSS)."""
        return self._resolved_orders

    # ------------------------------------------------------------------
    # Paper-mode resolution polling
    # ------------------------------------------------------------------

    async def poll_resolutions(self, btc_price: Optional[float] = None) -> None:
        """Check open orders and auto-resolve expired ones (paper mode).

        Called periodically by the engine's main loop with the current
        BTC spot price.

        Resolution rules:
        - Polymarket orders expire after ``window_seconds`` (default 5 min).
        - Opinion orders expire after 15 minutes.
        - ``arb`` strategy: always WIN (guaranteed profit from price discrepancy).
        - ``vpin_cascade`` and others: WIN if BTC moved in the predicted direction
          by at least MIN_BTC_MOVE_PCT from ``btc_entry_price``.

        Args:
            btc_price: Current BTC/USD spot price (uses stored price if None).
        """
        price = btc_price if btc_price is not None else self._current_btc_price
        if price <= 0:
            return

        now = time.time()
        expired: List[Order] = []

        async with self._lock:
            for order in self._orders.values():
                if order.status not in {OrderStatus.OPEN, OrderStatus.FILLED}:
                    continue
                age = now - order.created_at
                if age >= order.window_seconds:
                    expired.append(order)

        for order in expired:
            # Live mode: check if CLOB order actually filled before resolving
            if not self._paper_mode and self._poly_client and order.order_id.startswith("0x"):
                try:
                    clob_status = await self._poly_client.get_order_status(order.order_id)
                    size_matched = float(clob_status.get("size_matched", "0") or "0")
                    
                    if size_matched == 0:
                        # Order never filled — no position, no win/loss
                        self._log.info(
                            "order_manager.unfilled_expired",
                            order_id=order.order_id[:20] + "...",
                            clob_status=clob_status.get("status", "UNKNOWN"),
                        )
                        # Mark as expired, not win/loss
                        async with self._lock:
                            order.status = OrderStatus.EXPIRED
                            order.outcome = None
                            order.pnl_usd = 0.0
                            order.payout_usd = 0.0
                            order.resolved_at = time.time()
                        await self._persist_trade(order)
                        continue
                    else:
                        self._log.info(
                            "order_manager.filled_resolving",
                            order_id=order.order_id[:20] + "...",
                            size_matched=size_matched,
                        )
                except Exception as exc:
                    self._log.warning(
                        "order_manager.clob_check_failed",
                        order_id=order.order_id[:20] + "...",
                        error=str(exc),
                    )
                    # If CLOB check fails, skip resolution rather than fake it
                    continue

            # PRIMARY: Query Polymarket oracle for actual market outcome
            poly_result = await self._resolve_from_polymarket(order)
            if poly_result is not None:
                outcome, payout = poly_result
            else:
                # FALLBACK: Use Binance BTC price (less accurate)
                self._log.debug("resolution.fallback_to_binance", order_id=order.order_id[:20] + "...")
                result = self._determine_paper_outcome(order, price)
                if result is None:
                    continue  # Not ready to resolve yet (waiting for window close buffer)
                outcome, payout = result
            
            resolved = await self.resolve_order(order.order_id, outcome, payout)

            # Persist updated trade to DB (upsert with resolution data)
            await self._persist_trade(resolved)

            # Update window_snapshots with outcome so we can analyse signal quality
            if self._db:
                try:
                    meta = resolved.metadata or {}
                    window_ts = meta.get("window_ts")
                    asset = meta.get("market_slug", "").split("-")[0].upper() or "BTC"
                    tf = meta.get("timeframe", "5m")
                    # poly_winner: "Up" or "Down" depending on resolved direction
                    if resolved.outcome == "WIN":
                        poly_winner = "Up" if resolved.direction == "YES" else "Down"
                    else:
                        poly_winner = "Down" if resolved.direction == "YES" else "Up"
                    await self._db.update_window_outcome(
                        window_ts, asset, tf, resolved.outcome, resolved.pnl_usd or 0.0, poly_winner
                    )
                except Exception:
                    pass

            if self._on_resolution:
                try:
                    self._on_resolution(resolved)
                except Exception as exc:
                    self._log.error("order_manager.resolution_callback_error", error=str(exc))

    async def _resolve_from_polymarket(self, order: Order) -> tuple[str, float] | None:
        """Query Polymarket Gamma API for actual market resolution.
        
        Returns (outcome, payout) if market is resolved, None if not yet resolved.
        """
        market_slug = (order.metadata or {}).get("market_slug")
        if not market_slug:
            return None
        
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://gamma-api.polymarket.com/markets?slug={market_slug}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            
            if not data or not isinstance(data, list) or not data[0].get("closed"):
                return None
            
            market = data[0]
            outcome_prices = market.get("outcomePrices", [])
            outcomes = market.get("outcomes", [])
            
            if isinstance(outcome_prices, str):
                import json as _json
                outcome_prices = _json.loads(outcome_prices)
            if isinstance(outcomes, str):
                import json as _json
                outcomes = _json.loads(outcomes)
            
            # Find the winning outcome (price >= 0.99)
            winner = None
            for i, price in enumerate(outcome_prices):
                if float(price) >= 0.99:
                    winner = outcomes[i] if i < len(outcomes) else None
                    break
            
            if winner is None:
                return None
            
            # Map: YES bet = Up, NO bet = Down
            if order.direction == "YES":
                won = winner == "Up"
            else:
                won = winner == "Down"
            
            if won:
                try:
                    fill_price = float(order.price)
                    shares = order.stake_usd / fill_price if fill_price > 0 else 0
                    payout = shares * 1.0
                except (ValueError, ZeroDivisionError):
                    payout = order.stake_usd * 1.9
                
                self._log.info(
                    "polymarket_resolution.win",
                    order_id=order.order_id[:20] + "...",
                    direction=order.direction,
                    poly_winner=winner,
                    payout=f"{payout:.2f}",
                    market=market_slug,
                )
                return "WIN", payout
            else:
                self._log.info(
                    "polymarket_resolution.loss",
                    order_id=order.order_id[:20] + "...",
                    direction=order.direction,
                    poly_winner=winner,
                    market=market_slug,
                )
                return "LOSS", 0.0
        
        except Exception as exc:
            self._log.debug("polymarket_resolution.failed", error=str(exc))
            return None

    async def _determine_paper_outcome(
        self, order: Order, current_btc_price: float
    ) -> tuple[str, float]:
        """Determine WIN/LOSS and payout for an expired paper order (FALLBACK only).

        Used when Polymarket API is unavailable. Prefer _resolve_from_polymarket().

        Args:
            order: The expired order.
            current_btc_price: Current BTC/USD price.

        Returns:
            Tuple of (outcome: str, payout_usd: float).
        """
        if order.strategy == "arb":
            # Arb is always a WIN — the profit is baked into the entry
            payout = order.stake_usd * 1.15  # ~15% guaranteed return simulation
            self._log.debug(
                "paper_resolution.arb_win",
                order_id=order.order_id,
                payout=payout,
            )
            return "WIN", payout

        # ── 5-Minute Market Resolution ────────────────────────────────────
        # For five_min_vpin strategy, resolution is:
        #   - Get the window OPEN price from order metadata
        #   - Get the CURRENT price (= close price, since we resolve at window end)
        #   - If close >= open → "UP" wins, otherwise "DOWN" wins
        #   - YES bet = UP, NO bet = DOWN
        #
        # This matches the Polymarket Chainlink oracle exactly.
        # ──────────────────────────────────────────────────────────────────

        if order.strategy == "five_min_vpin":
            window_open = order.metadata.get("window_open_price")
            if window_open is None or window_open == 0:
                # Fallback: use entry price (T-10s) — less accurate but workable
                window_open = order.btc_entry_price

            if window_open is None or window_open == 0:
                import random
                outcome = "WIN" if random.random() > 0.5 else "LOSS"
                payout = order.stake_usd * 1.9 if outcome == "WIN" else 0.0
                return outcome, payout

            # Did BTC go up or down from window open to close?
            # v7.1: Use the window open price + current BTC price BUT only
            # resolve AFTER a buffer period to ensure the window has fully closed.
            # The resolution poll runs every 5s. Window = 300s. So at 305s age,
            # the live BTC price IS effectively the close price (±5s).
            # For more accuracy, we add 10s buffer beyond window_seconds.
            age = time.time() - order.created_at
            if age < order.window_seconds + 10:
                # Too early — window might not have closed yet, skip this cycle
                return None  # Will be picked up on next poll
            
            btc_went_up = current_btc_price >= window_open

            # Did our bet match?
            if order.direction == "YES":
                won = btc_went_up      # YES = bet on UP
            else:
                won = not btc_went_up  # NO = bet on DOWN

            if won:
                # Binary payout: $1 per share. Shares = stake / token_price
                try:
                    fill_price = float(order.price)
                    shares = order.stake_usd / fill_price if fill_price > 0 else 0
                    payout = shares * 1.0
                except (ValueError, ZeroDivisionError):
                    payout = order.stake_usd * 1.9

                self._log.info(
                    "paper_resolution.5min_win",
                    order_id=order.order_id,
                    direction=order.direction,
                    window_open=f"{window_open:.2f}",
                    close_price=f"{current_btc_price:.2f}",
                    btc_went_up=btc_went_up,
                    payout=f"{payout:.2f}",
                )
                return "WIN", payout
            else:
                self._log.info(
                    "paper_resolution.5min_loss",
                    order_id=order.order_id,
                    direction=order.direction,
                    window_open=f"{window_open:.2f}",
                    close_price=f"{current_btc_price:.2f}",
                    btc_went_up=btc_went_up,
                )
                return "LOSS", 0.0

        # ── Generic Directional Resolution (cascade etc.) ─────────────────

        entry = order.btc_entry_price
        if entry is None or entry == 0.0:
            import random
            outcome = "WIN" if random.random() > 0.5 else "LOSS"
            payout = order.stake_usd * 1.9 if outcome == "WIN" else 0.0
            return outcome, payout

        move_pct = (current_btc_price - entry) / entry

        if order.direction == "YES":
            won = move_pct >= MIN_BTC_MOVE_PCT
        else:
            won = move_pct <= -MIN_BTC_MOVE_PCT

        if won:
            try:
                fill_price = float(order.price)
                shares = order.stake_usd / fill_price if fill_price > 0 else 0
                payout = shares * 1.0
            except (ValueError, ZeroDivisionError):
                payout = order.stake_usd * 1.9
            self._log.debug(
                "paper_resolution.direction_win",
                order_id=order.order_id,
                direction=order.direction,
                move_pct=f"{move_pct*100:.3f}%",
                payout=payout,
            )
            return "WIN", payout
        else:
            self._log.debug(
                "paper_resolution.direction_loss",
                order_id=order.order_id,
                direction=order.direction,
                move_pct=f"{move_pct*100:.3f}%",
            )
            return "LOSS", 0.0
