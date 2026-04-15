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
from typing import Dict, List, Optional

import structlog

from domain.entities import Order, OrderStatus  # noqa: F401 — re-export for backward compat
from config.constants import (  # noqa: F401 — re-export for backward compat
    POLY_WINDOW_SECONDS,
    OPINION_WINDOW_SECONDS,
    MIN_BTC_MOVE_PCT,
)

__all__ = [
    "OrderManager",
    "Order",
    "OrderStatus",
    "POLY_WINDOW_SECONDS",
    "OPINION_WINDOW_SECONDS",
    "MIN_BTC_MOVE_PCT",
]

logger = structlog.get_logger(__name__)


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
        self._order_id_aliases: Dict[str, str] = {}  # retry_id → original_order_id
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

    async def recover_open_trades(self, db) -> int:
        """On startup, load OPEN trades from DB and register them for resolution polling.

        Fetches all OPEN trades created in the last 24 hours and re-registers them
        in the in-memory order book so poll_resolutions() can resolve them.
        Trades older than 10 minutes are also reconciled against the Polymarket CLOB:
          - CANCELED/EXPIRED → marked EXPIRED in DB
          - MATCHED (has fill) → recorded as filled and left for oracle resolution

        Args:
            db: DBClient instance (must already be connected).

        Returns:
            Number of trades successfully recovered and registered.
        """
        open_rows = await db.get_open_trades(hours_back=24)
        if not open_rows:
            self._log.info("order_manager.recover.no_open_trades")
            return 0

        recovered = 0
        now = time.time()

        for row in open_rows:
            order_id = row["order_id"]

            # Skip if already tracked (e.g. placed this session before recovery ran)
            async with self._lock:
                if order_id in self._orders:
                    continue

            # Build Order from DB row
            import json as _json
            meta_raw = row.get("metadata")
            if isinstance(meta_raw, str):
                try:
                    meta = _json.loads(meta_raw)
                except Exception:
                    meta = {}
            elif isinstance(meta_raw, dict):
                meta = meta_raw
            else:
                meta = {}

            created_at_raw = row.get("created_at")
            if created_at_raw is None:
                created_ts = now
            elif hasattr(created_at_raw, "timestamp"):
                created_ts = created_at_raw.timestamp()
            else:
                created_ts = float(created_at_raw)

            age_secs = now - created_ts

            # ── Resolve stale paper trades on restart ──────────────────
            # Paper trade IDs (paper-fak-*, manual-paper-*) have no CLOB presence.
            # Instead of expiring them blind, try to resolve against window_snapshots:
            # if the oracle outcome is known, record WIN/LOSS properly so the data
            # is available for strategy analysis. Fall back to EXPIRED only if no
            # oracle data is available.
            if age_secs > 600 and not order_id.startswith("0x"):
                try:
                    direction = row.get("direction") or row.get("side") or "UP"
                    oracle_outcome = await db.get_oracle_outcome_for_trade(row)
                    if oracle_outcome:
                        pnl = row.get("stake_usd", 0) or 0
                        won = oracle_outcome.upper() == direction.upper()
                        await db.resolve_paper_trade(
                            order_id=order_id,
                            outcome="WIN" if won else "LOSS",
                            pnl_usd=float(pnl) * 0.95 if won else -float(pnl),
                            resolved_direction=oracle_outcome,
                        )
                        self._log.info(
                            "order_manager.recover.paper_resolved",
                            order_id=order_id[:32],
                            outcome="WIN" if won else "LOSS",
                            direction=direction,
                            oracle=oracle_outcome,
                        )
                    else:
                        await db.mark_trade_expired(order_id)
                        self._log.info(
                            "order_manager.recover.paper_expired_no_oracle",
                            order_id=order_id[:32],
                            age_secs=int(age_secs),
                        )
                except Exception as exc:
                    self._log.warning(
                        "order_manager.recover.paper_resolve_error",
                        order_id=order_id[:32],
                        error=str(exc)[:100],
                    )
                    await db.mark_trade_expired(order_id)
                continue

            # ── Reconcile stale live trades (> 10 minutes old) against CLOB ──
            if age_secs > 600 and self._poly_client and order_id.startswith("0x"):
                try:
                    clob_status = await self._poly_client.get_order_status(order_id)
                    clob_state = (clob_status.get("status") or "").upper()
                    size_matched = float(clob_status.get("size_matched", "0") or "0")

                    if clob_state in ("CANCELED", "UNMATCHED", "EXPIRED") and size_matched == 0:
                        # Never filled — mark expired in DB, skip registration
                        await db.mark_trade_expired(order_id)
                        self._log.info(
                            "order_manager.recover.expired",
                            order_id=order_id[:24] + "..." if len(order_id) > 24 else order_id,
                            clob_state=clob_state,
                            age_secs=int(age_secs),
                        )
                        continue
                    elif size_matched > 0 and clob_state != "OPEN":
                        # Partially or fully matched — update metadata so oracle can resolve
                        meta["fill_price"] = clob_status.get("price") or meta.get("fill_price")
                        meta["fill_size"] = size_matched
                        meta["execution_mode"] = "live"
                        self._log.info(
                            "order_manager.recover.matched",
                            order_id=order_id[:24] + "..." if len(order_id) > 24 else order_id,
                            size_matched=size_matched,
                        )
                    # If still OPEN on CLOB — fall through and register normally
                except Exception as exc:
                    self._log.warning(
                        "order_manager.recover.clob_check_failed",
                        order_id=order_id[:24] + "..." if len(order_id) > 24 else order_id,
                        error=str(exc),
                    )
                    # Proceed with registration anyway — oracle will resolve it

            order = Order(
                order_id=order_id,
                venue=row.get("venue") or "polymarket",
                strategy=row.get("strategy") or "five_min_vpin",
                direction=row.get("direction") or "YES",
                price=str(row.get("entry_price") or "0.5"),
                stake_usd=float(row.get("stake_usd") or 0),
                fee_usd=float(row.get("fee_usd") or 0),
                status=OrderStatus.OPEN,
                created_at=created_ts,
                market_id=row.get("market_slug") or meta.get("market_slug", ""),
                metadata=meta,
            )

            # Register without re-persisting (already in DB)
            async with self._lock:
                self._orders[order_id] = order
                self._total_orders += 1

            recovered += 1
            self._log.info(
                "order_manager.recovered",
                order_id=order_id[:24] + "..." if len(order_id) > 24 else order_id,
                strategy=order.strategy,
                direction=order.direction,
                age_mins=f"{age_secs / 60:.1f}",
            )

        self._log.info("order_manager.recovery_complete", recovered=recovered, total_found=len(open_rows))
        return recovered

    async def register_retry_order_id(self, retry_id: str, original_id: str) -> None:
        """Register a retry order ID as an alias for the original order.

        When FOK fails and a GTC/bumped retry is placed, the retry gets a NEW
        CLOB order ID. This maps retry_id → original order so that resolve_order()
        works for both the original ID and the retry ID.

        Args:
            retry_id: The new CLOB order ID assigned to the retry order.
            original_id: The original order ID already tracked in self._orders.
        """
        async with self._lock:
            self._order_id_aliases[retry_id] = original_id
            self._log.info(
                "order_manager.retry_alias_registered",
                retry_id=retry_id[:20] + "..." if len(retry_id) > 20 else retry_id,
                original_id=original_id[:20] + "..." if len(original_id) > 20 else original_id,
            )

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
            # If this is a retry order ID, resolve via the original order's record
            canonical_id = self._order_id_aliases.get(order_id, order_id)
            order = self._orders[canonical_id]  # raises KeyError if missing
            # Update the canonical key used for logging
            order_id = canonical_id
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
                # Check for resolution after 240s (oracle resolves ~4min post-open)
                # instead of waiting for full window_seconds (300s)
                resolve_after = min(order.window_seconds, 240)
                if age >= resolve_after:
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
            # ALWAYS resolve from Polymarket oracle — paper and live
            # Polymarket IS the market. Binance price direction != oracle outcome.
            poly_result = await self._resolve_from_polymarket(order)
            if poly_result is not None:
                outcome, payout = poly_result
            else:
                # Oracle hasn't resolved yet — wait and retry next poll cycle
                self._log.debug("resolution.waiting_for_polymarket", order_id=order.order_id[:20] + "...")
                continue
            
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
                    # v8.1.2: Update window_predictions with oracle result
                    try:
                        await self._db.update_window_prediction_outcome(
                            window_ts, asset, poly_winner
                        )
                    except Exception:
                        pass
                    # Save close prices from Chainlink + Tiingo + Binance at resolution time (v7.2)
                    try:
                        _cl_close = await self._db.get_latest_chainlink_price(asset)
                        _ti_close = await self._db.get_latest_tiingo_price(asset)
                        # Binance close: use btc_price passed to poll_resolutions
                        _bn_close = btc_price  # may be None if not provided
                        # Direction match: did Chainlink direction agree with Binance at resolution?
                        _cl_bn_match = None
                        if _cl_close and _bn_close:
                            # Compare relative to window open price for direction context
                            # (Use Chainlink close vs Binance close direction vs order direction)
                            _cl_dir = "UP" if resolved.direction == "YES" else "DOWN"
                            # Chainlink vs Binance close: agree if price difference is small or same sign
                            _cl_bn_match = abs(_cl_close - _bn_close) / max(_bn_close, 1.0) < 0.005
                        # Resolution delay: time from order created to resolved
                        _res_delay = None
                        if resolved.resolved_at and hasattr(resolved, 'created_at') and resolved.created_at:
                            _res_delay = int(resolved.resolved_at - resolved.created_at)
                        await self._db.update_window_prices(
                            window_ts, asset, tf,
                            chainlink_close=_cl_close,
                            tiingo_close=_ti_close,
                            poly_resolved_outcome=poly_winner,
                        )
                        # Update extra resolution columns (binance_close, direction match, delay)
                        await self._db.update_window_resolution_extras(
                            window_ts, asset, tf,
                            binance_close=_bn_close,
                            chainlink_binance_direction_match=_cl_bn_match,
                            resolution_delay_secs=_res_delay,
                        )
                    except Exception:
                        pass
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
