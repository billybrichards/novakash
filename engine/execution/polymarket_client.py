"""
Polymarket CLOB client — paper mode + live mode.

Paper mode simulates order fills with realistic slippage.
Live mode uses the py-clob-client to sign and submit real orders.

Safety guards for live mode:
- Requires LIVE_TRADING_ENABLED=true environment variable
- Hard cap of $50 per single trade
- Logs a WARNING on first live trade
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import structlog


# ─── POLY-SOT data classes ──────────────────────────────────────────────────
#
# `PolyOrderStatus` is the source-of-truth payload returned by the new
# `get_order_status_sot` and `list_recent_orders` methods. It is a thin,
# typed wrapper that hides Polymarket's wire-format quirks (lowercase vs
# uppercase status, multiple field names for size_matched, makingAmount vs
# takingAmount semantics) so the SOT reconciler can compare against the
# engine's local manual_trades row without re-implementing parsing.
#
# Status values are normalised to a small fixed alphabet:
#     'pending' | 'matched' | 'filled' | 'cancelled' | 'rejected' | 'unknown'
# 'matched' and 'filled' are both terminal — Polymarket uses 'matched' for
# instant fills (FAK/FOK) and 'filled' for resting GTC orders that fully
# matched. The reconciler treats them identically when comparing against
# engine state.


@dataclass(frozen=True)
class PolyOrderStatus:
    """Normalised Polymarket CLOB order status used by the SOT reconciler.

    Named ``PolyOrderStatus`` rather than ``OrderStatus`` to avoid colliding
    with the existing ``execution.order_manager.OrderStatus`` enum which is
    already exported from ``execution.__init__`` and represents the engine's
    own per-trade lifecycle (OPEN/FILLED/RESOLVED_WIN/RESOLVED_LOSS).
    """

    order_id: str
    status: str  # 'pending'|'matched'|'filled'|'cancelled'|'rejected'|'unknown'
    fill_price: Optional[float]
    fill_size: Optional[float]
    timestamp: Optional[datetime]
    raw: Optional[dict] = None

    @property
    def is_terminal(self) -> bool:
        """True if Polymarket considers this order done (filled OR cancelled)."""
        return self.status in {"matched", "filled", "cancelled", "rejected"}

    @property
    def is_filled(self) -> bool:
        """True if Polymarket has booked at least some shares for this order."""
        return self.status in {"matched", "filled"} or (
            self.fill_size is not None and self.fill_size > 0
        )


logger = structlog.get_logger(__name__)

# 5-minute prediction windows
POLY_WINDOW_SECONDS = 300

# Simulated paper balance (USD)
_PAPER_BALANCE_USD = 10_000.0

# Safety: maximum single-trade size in live mode (USD)
LIVE_MAX_TRADE_USD = 50.0


class PolymarketClient:
    """
    Client for interacting with the Polymarket CLOB API.

    In paper mode all orders are simulated locally with realistic slippage.
    In live mode the CLOB REST API is used via py-clob-client.

    Safety guards (live mode only):
    - Requires ``LIVE_TRADING_ENABLED=true`` environment variable at construction.
    - Hard cap of $50 per single order; raises ``ValueError`` if exceeded.
    - Emits a ``RuntimeWarning`` and a structured WARNING log on the first live
      order placed in this process.

    Args:
        private_key: Ethereum private key for signing CLOB orders.
        api_key: Polymarket API key.
        api_secret: Polymarket API secret.
        api_passphrase: Polymarket API passphrase.
        funder_address: Address that funds positions.
        paper_mode: If True (default), simulate all trades locally.
    """

    def __init__(
        self,
        private_key: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        funder_address: str,
        paper_mode: bool = True,
    ) -> None:
        self._private_key = private_key
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._funder_address = funder_address
        self.paper_mode = paper_mode

        # Paper-mode state
        self._paper_balance: float = _PAPER_BALANCE_USD
        self._paper_orders: dict[str, dict] = {}

        # Live-mode client handle (initialised in connect())
        self._clob_client: Optional[object] = None

        # Safety: track whether the first live-trade warning has been emitted
        self._live_first_trade_warned: bool = False

        self._log = logger.bind(component="polymarket_client", paper_mode=paper_mode)

        # Safety check: refuse to construct in live mode without explicit opt-in
        if not paper_mode:
            live_enabled = os.environ.get("LIVE_TRADING_ENABLED", "").strip().lower()
            if live_enabled != "true":
                # Fallback: check .env file
                from pathlib import Path

                _env_file = Path(__file__).parent.parent / ".env"
                if _env_file.exists():
                    with open(_env_file) as f:
                        for line in f:
                            if line.startswith("LIVE_TRADING_ENABLED="):
                                live_enabled = line.split("=", 1)[1].strip().lower()
                                break
            if live_enabled != "true":
                raise EnvironmentError(
                    "Live trading requires LIVE_TRADING_ENABLED=true environment variable. "
                    "Set this explicitly to confirm intent before enabling live mode."
                )

    @property
    def clob_client(self):
        """Public read-only accessor for the underlying ClobClient handle.

        Returns None in paper mode or before connect() is called.
        Prefer get_clob_order_book() for order book queries — it works in both
        paper and live mode. This accessor exists for legacy callers that
        need direct ClobClient access.
        """
        return self._clob_client

    async def get_clob_order_book(self, token_id: str):
        """Fetch CLOB order book for a token — works in paper AND live mode.

        Args:
            token_id: Polymarket token ID (UP or DOWN token)

        Returns:
            {"up_best_bid": float, "up_best_ask": float, ...} or None if no liquidity
        """
        # Both paper and live mode use the real CLOB API — order book is public,
        # no auth required. Paper mode = fake money, real market data.
        # Montreal rule: this method is only called from the Montreal engine.
        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        def _fetch_book():
            book = self._clob_client.get_order_book(token_id)
            bids = (
                [(float(b.price), float(b.size)) for b in book.bids[:5]]
                if book.bids
                else []
            )
            asks = (
                [(float(a.price), float(a.size)) for a in book.asks[:5]]
                if book.asks
                else []
            )
            return {"bids": bids, "asks": asks}

        return await asyncio.to_thread(_fetch_book)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Initialise the CLOB client connection.

        Paper mode just logs the startup.
        Live mode constructs and authenticates the py-clob-client.
        """
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        if self.paper_mode:
            # Paper mode: init read-only CLOB client for real market data.
            # Order book reads are unauthenticated — no creds needed.
            self._clob_client = ClobClient(
                host="https://clob.polymarket.com",
                key=self._private_key,
                chain_id=137,
            )
            self._log.info("polymarket_client.connected", mode="paper", clob="read_only")
            return

        # Build base client args
        client_kwargs = dict(
            host="https://clob.polymarket.com",
            key=self._private_key,
            chain_id=137,  # Polygon mainnet
            signature_type=int(os.environ.get("POLY_SIGNATURE_TYPE", "2")),
            funder=self._funder_address,
        )

        # Use provided API creds, or auto-derive from private key
        if self._api_key and self._api_secret and self._api_passphrase:
            client_kwargs["creds"] = ApiCreds(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_passphrase,
            )
            self._clob_client = ClobClient(**client_kwargs)
        else:
            # Derive creds from private key (first-time setup)
            self._clob_client = ClobClient(**client_kwargs)
            self._clob_client.set_api_creds(
                self._clob_client.create_or_derive_api_creds()
            )
            self._log.info("polymarket_client.creds_derived", mode="live")

        self._log.info("polymarket_client.connected", mode="live")

    # ------------------------------------------------------------------
    # High-level convenience — called by strategies
    # ------------------------------------------------------------------

    async def place_order(
        self,
        market_slug: str,
        direction: str,
        price: Decimal,
        stake_usd: float,
        token_id: Optional[str] = None,
    ) -> str:
        """Place a directional order on a binary market.

        Args:
            market_slug: Human-readable market identifier, e.g.
                         "btc-updown-5m-1711900800".
            direction: "YES" or "NO".
            price: Expected fill price in [0, 1].
            stake_usd: Notional USD amount to risk.
            token_id: CLOB token ID for the outcome token. Required for live
                      mode; ignored in paper mode.

        Returns:
            Order ID string (paper: "paper-<uuid>", live: CLOB order ID).
        """
        direction = direction.upper()
        if direction not in {"YES", "NO"}:
            raise ValueError(f"direction must be YES or NO, got {direction!r}")

        self._log.info(
            "place_order.requested",
            market_slug=market_slug,
            direction=direction,
            price=str(price),
            stake_usd=stake_usd,
        )

        if self.paper_mode:
            return await self._paper_place_order(
                market_slug, direction, price, stake_usd
            )

        return await self._live_place_order(
            market_slug, direction, price, stake_usd, token_id
        )

    async def _paper_place_order(
        self,
        market_slug: str,
        direction: str,
        price: Decimal,
        stake_usd: float,
    ) -> str:
        """Simulate a paper-mode order fill with realistic slippage."""
        # ±0.5% random slippage
        slippage = Decimal(str(random.uniform(-0.005, 0.005)))
        fill_price = max(Decimal("0.01"), min(Decimal("0.99"), price + slippage))

        shares = float(stake_usd) / float(fill_price)
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        ts = time.time()

        self._paper_orders[order_id] = {
            "order_id": order_id,
            "market_slug": market_slug,
            "direction": direction,
            "requested_price": str(price),
            "fill_price": str(fill_price),
            "stake_usd": stake_usd,
            "shares": shares,
            "status": "FILLED",
            "created_at": ts,
            "filled_at": ts,
        }
        self._paper_balance -= stake_usd

        self._log.info(
            "place_order.paper_filled",
            order_id=order_id,
            fill_price=str(fill_price),
            slippage_pct=f"{float(slippage) * 100:.3f}%",
            shares=f"{shares:.4f}",
        )
        return order_id

    async def _live_place_order(
        self,
        market_slug: str,
        direction: str,
        price: Decimal,
        stake_usd: float,
        token_id: Optional[str] = None,
    ) -> str:
        """Place a real order on Polymarket CLOB.

        Args:
            market_slug: Human-readable market identifier.
            direction: "YES" or "NO".
            price: Limit price in [0, 1].
            stake_usd: Notional USD to risk.
            token_id: CLOB outcome token ID (required).

        Returns:
            CLOB order ID string.

        Raises:
            RuntimeError: If the CLOB client is not connected.
            ValueError: If token_id is missing or stake exceeds the safety cap.
        """
        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        if not token_id:
            raise ValueError(
                f"token_id is required for live order placement "
                f"(market_slug={market_slug!r}, direction={direction!r})"
            )

        # Safety cap
        if stake_usd > LIVE_MAX_TRADE_USD:
            raise ValueError(
                f"Live trade stake ${stake_usd:.2f} exceeds safety cap "
                f"${LIVE_MAX_TRADE_USD:.2f}. Reduce stake or raise LIVE_MAX_TRADE_USD."
            )

        # First-live-trade warning
        if not self._live_first_trade_warned:
            self._live_first_trade_warned = True
            warnings.warn(
                "First live Polymarket trade being placed — verify manually on "
                "polymarket.com/portfolio",
                RuntimeWarning,
                stacklevel=2,
            )
            self._log.warning(
                "place_order.first_live_trade",
                market_slug=market_slug,
                direction=direction,
                price=str(price),
                stake_usd=stake_usd,
                token_id=token_id[:20] + "..." if len(token_id) > 20 else token_id,
            )

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        # Calculate size (number of shares = stake / price)
        client = self._clob_client

        # ── Strategy: Limit orders ONLY at Gamma API price ─────────────
        # REVERTED from FAK market orders (2026-04-02 afternoon).
        #
        # WHY: Market orders (FAK) bought tokens at 88-98¢ on thin books.
        # At 98¢ you need 98% accuracy to break even — impossible.
        # Limit orders at Gamma price (38-52¢) gave 89% win rate and
        # +$218 profit in the morning session.
        #
        # TRADEOFF: ~40-60% fill rate (some orders sit unfilled) but
        # every fill has excellent risk/reward. Better to miss trades
        # than pay terrible prices.
        #
        # MAX PRICE CAP: from env var (default 0.80 for v7.1)
        # The strategy already caps at FIVE_MIN_MAX_ENTRY_PRICE but we double-check here
        token_price_f = float(price)
        is_15m = "15m" in market_slug
        max_price = (
            float(os.environ.get("FIFTEEN_MIN_MAX_ENTRY_PRICE", "0.80"))
            if is_15m
            else float(os.environ.get("FIVE_MIN_MAX_ENTRY_PRICE", "0.80"))
        )
        if token_price_f > max_price:
            self._log.warning(
                "place_order.price_too_high",
                price=str(price),
                max_price=max_price,
                market_slug=market_slug,
            )
            raise ValueError(
                f"Token price {price} exceeds {int(max_price * 100)}¢ cap — skipping"
            )
        if token_price_f < 0.30:
            self._log.warning(
                "place_order.price_too_low",
                price=str(price),
                market_slug=market_slug,
            )
            raise ValueError(f"Token price {price} below 30¢ floor — skipping")

        size = round(stake_usd / float(price), 2)

        # ── GTD expiry: auto-expire when the 5m/15m window closes ──────
        # Extract window_ts from market_slug: "btc-updown-5m-1775256000"
        # Window closes at window_ts + duration (300s for 5m, 900s for 15m)
        expiration = 0
        try:
            parts = market_slug.split("-")
            window_ts = int(parts[-1])
            duration = 900 if is_15m else 300
            expiration = (
                window_ts + duration + 120
            )  # +2min buffer for Polymarket 1min threshold
        except (ValueError, IndexError):
            pass  # Fallback to no expiry (GTC)

        # ── Order strategy: Single GTC limit at bestAsk + 2¢ ──────────────
        #
        # Proven approach (Apr 2 morning: 89% WR, +$218):
        #   - Single GTC limit at Gamma bestAsk + small bump
        #   - NO retry, NO price bumping after initial order
        #   - Hard cap prevents overpaying
        #   - Accept ~40% fill rate at good prices
        #   - "Fill rate doesn't matter if fills are at bad prices"
        #
        # Lesson from Apr 2 afternoon: FAK/FOK at market swept book to 88-98¢ = disaster
        # Lesson from Apr 5-6 live: retry bumping caused $0.745 fills = disaster
        #
        # This approach: ONE order, good price, accept miss if no fill.

        # v8.1: GTC limit price = strategy's dynamic cap for this offset.
        # The strategy passes the correct per-window cap via the `price` arg
        # (which comes from v81_entry_cap). We use that directly as our limit.
        # This replaces the old env-var-based pricing modes.
        PRICE_FLOOR = float(os.environ.get("PRICE_FLOOR", "0.30"))

        # The price from strategy is the CLOB best ask or Gamma price.
        # We submit a GTC at the window's dynamic cap so we fill at market
        # price (which will be ≤ cap). The cap is passed via FIVE_MIN_MAX_ENTRY_PRICE
        # or inferred from the strategy signal.
        limit_price = round(float(price), 4)
        limit_price = max(limit_price, PRICE_FLOOR)

        order_size = round(stake_usd / limit_price, 2)
        # Fix maker_amount precision: price * size must have ≤ 2 decimal places
        for _adj in range(100):
            _maker = round(limit_price * order_size, 6)
            if abs(_maker - round(_maker, 2)) < 1e-9:
                break
            order_size -= 0.01
        # Enforce Polymarket minimum order size (5 shares)
        order_size = max(order_size, 5.0)

        _order_type = OrderType.GTD if expiration > 0 else OrderType.GTC

        order_args = OrderArgs(
            token_id=token_id,
            price=limit_price,
            size=order_size,
            side=BUY,
            expiration=expiration,
        )

        def _sign_and_submit():
            # Single GTC/GTD limit order — no FOK, no retry, no bumping
            signed = client.create_order(order_args)
            return client.post_order(signed, _order_type)

        self._log.info(
            "place_order.order_strategy",
            limit_price=f"${limit_price:.4f}",
            strategy_price=f"${float(price):.4f}",
            floor=f"${PRICE_FLOOR:.2f}",
            size=f"{order_size:.2f}",
            order_type=str(_order_type),
            seconds_to_expiry=expiration - int(time.time())
            if expiration > 0
            else "none",
        )

        response = await asyncio.to_thread(_sign_and_submit)

        # Response can be a dict or an object — handle both
        if isinstance(response, dict):
            order_id = (
                response.get("orderID")
                or response.get("id")
                or f"live-{uuid.uuid4().hex[:12]}"
            )
        else:
            order_id = (
                getattr(response, "orderID", None)
                or getattr(response, "id", None)
                or f"live-{uuid.uuid4().hex[:12]}"
            )

        self._log.info(
            "place_order.live_submitted",
            order_id=order_id,
            market_slug=market_slug,
            direction=direction,
            price=str(price),
            stake_usd=stake_usd,
            token_id=token_id[:20] + "..." if len(token_id) > 20 else token_id,
        )

        return order_id

    # ------------------------------------------------------------------
    # FOK Ladder helpers — v8.0 Phase 2
    # ------------------------------------------------------------------

    async def get_clob_best_ask(self, token_id: str) -> float:
        """Query the CLOB order book and return the lowest (best) ask price.

        The CLOB book ``asks`` list is sorted descending — we sort ascending
        and take the first element to get the best ask.

        Paper mode returns a simulated price around 0.50.

        Args:
            token_id: CLOB outcome token ID.

        Returns:
            Best ask price as a float.

        Raises:
            RuntimeError: If live mode and CLOB client is not connected.
            ValueError: If the order book has no ask-side liquidity.
        """
        if self.paper_mode:
            import random

            simulated = round(random.uniform(0.40, 0.65), 4)
            self._log.debug(
                "get_clob_best_ask.paper",
                token_id=token_id[:20] + "...",
                simulated_ask=f"${simulated:.4f}",
            )
            return simulated

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        client = self._clob_client

        def _fetch_book():
            return client.get_order_book(token_id)

        book = await asyncio.to_thread(_fetch_book)

        # book.asks is sorted descending — lowest ask is the last element,
        # OR sort ascending and take first. Handle both list and object attrs.
        asks = getattr(book, "asks", None)
        if asks is None and isinstance(book, dict):
            asks = book.get("asks", [])

        if not asks:
            raise ValueError(f"No ask-side liquidity for token {token_id[:20]}...")

        # Sort ascending by price to reliably get the lowest ask
        try:
            sorted_asks = sorted(
                asks,
                key=lambda x: float(
                    getattr(x, "price", x.get("price", 0) if isinstance(x, dict) else 0)
                ),
            )
            best_ask = float(
                getattr(
                    sorted_asks[0],
                    "price",
                    sorted_asks[0].get("price")
                    if isinstance(sorted_asks[0], dict)
                    else sorted_asks[0],
                )
            )
        except (TypeError, AttributeError, IndexError) as exc:
            raise ValueError(
                f"Could not parse ask prices from order book: {exc}"
            ) from exc

        self._log.debug(
            "get_clob_best_ask.live",
            token_id=token_id[:20] + "...",
            best_ask=f"${best_ask:.4f}",
            ask_levels=len(asks),
        )
        return best_ask

    async def place_fok_order(
        self,
        token_id: str,
        price: float,
        size: float,
    ) -> dict:
        """Place a Fill-or-Kill (FOK) order on the CLOB.

        FOK orders are immediately executed or cancelled — no resting on the book.

        Paper mode simulates a fill at the requested price (always fills, because
        paper mode has unlimited simulated liquidity — realistic FOK testing
        requires live mode).

        Args:
            token_id: CLOB outcome token ID.
            price: Limit price in [0, 1].
            size: Number of shares.

        Returns:
            Dict: {filled: bool, size_matched: float, order_id: str}

        Raises:
            RuntimeError: If live mode and CLOB client is not connected.
        """
        if self.paper_mode:
            simulated_order_id = f"fok-paper-{uuid.uuid4().hex[:12]}"
            self._log.info(
                "place_fok_order.paper",
                token_id=token_id[:20] + "...",
                price=f"${price:.4f}",
                size=f"{size:.2f}",
                order_id=simulated_order_id,
            )
            return {
                "filled": True,
                "size_matched": size,
                "order_id": simulated_order_id,
            }

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        # Safety cap check
        stake_usd = price * size
        if stake_usd > LIVE_MAX_TRADE_USD:
            raise ValueError(
                f"FOK trade stake ${stake_usd:.2f} exceeds safety cap "
                f"${LIVE_MAX_TRADE_USD:.2f}."
            )

        # First-live-trade warning
        if not self._live_first_trade_warned:
            self._live_first_trade_warned = True
            import warnings

            warnings.warn(
                "First live Polymarket FOK trade being placed — verify manually on "
                "polymarket.com/portfolio",
                RuntimeWarning,
                stacklevel=2,
            )
            self._log.warning(
                "place_fok_order.first_live_trade",
                token_id=token_id[:20] + "..." if len(token_id) > 20 else token_id,
                price=f"${price:.4f}",
                size=f"{size:.2f}",
            )

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        client = self._clob_client

        # CLOB requires: price max 4 decimals, size (maker) max 2 decimals
        # Floor size to avoid taker amount exceeding 4 decimal precision
        import math

        _price = round(price, 4)
        _size = math.floor(size * 100) / 100  # Floor to 2 decimals
        # CLOB requires maker_amount (price*size) to have ≤ 2 decimal places
        for _adj in range(100):
            _maker = round(_price * _size, 6)
            if abs(_maker - round(_maker, 2)) < 1e-9:
                break
            _size -= 0.01
        _size = max(_size, 0.01)
        if _size <= 0:
            return {"filled": False, "size_matched": 0, "order_id": None}
        _size_str = f"{_size:.2f}"
        _price_str = f"{_price:.4f}"

        order_args = OrderArgs(
            token_id=token_id,
            price=float(_price_str),
            size=float(_size_str),
            side=BUY,
        )

        def _sign_and_submit():
            signed = client.create_order(order_args)
            return client.post_order(signed, OrderType.FOK)

        self._log.info(
            "place_fok_order.submitting",
            token_id=token_id[:20] + "...",
            price=f"${price:.4f}",
            size=f"{size:.2f}",
        )

        response = await asyncio.to_thread(_sign_and_submit)

        # CRITICAL BUG FIX (Apr 10): Polymarket CLOB response field names
        # Polymarket returns: success, orderID, status ("live"/"matched"/"delayed"/
        # "unmatched"), makingAmount, takingAmount, transactionsHashes, tradeIDs
        # The old code checked "size_matched" (doesn't exist) and uppercase status
        # ("MATCHED"/"FILLED") — both failed silently, causing the FOK ladder to
        # fire attempt 2 AND GTC fallback even when attempt 1 had filled.
        # Result: 77% of trades were multi-fills, overpaying ~$4.43/trade.

        def _get(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        order_id = (
            _get(response, "orderID")
            or _get(response, "id")
            or f"fok-live-{uuid.uuid4().hex[:12]}"
        )
        status_raw = _get(response, "status", "UNKNOWN") or "UNKNOWN"
        status = str(status_raw).lower()  # normalize: "matched" / "live" / "unmatched"
        success = bool(_get(response, "success", False))
        error_msg = _get(response, "errorMsg", "") or ""
        making_raw = _get(response, "makingAmount", "0")
        taking_raw = _get(response, "takingAmount", "0")

        try:
            making_amount = float(making_raw) if making_raw else 0.0
        except (ValueError, TypeError):
            making_amount = 0.0
        try:
            taking_amount = float(taking_raw) if taking_raw else 0.0
        except (ValueError, TypeError):
            taking_amount = 0.0

        # For a BUY order: makingAmount = USDC paid, takingAmount = shares received
        # size_matched should be the share quantity we received
        size_matched = taking_amount

        # Fill detection: lowercase "matched" status, or we actually received shares
        # "unmatched" = killed without fill, "live" = resting (shouldn't happen for FOK)
        filled = (status == "matched") or (size_matched > 0)

        self._log.info(
            "place_fok_order.result",
            token_id=token_id[:20] + "...",
            price=f"${price:.4f}",
            size=f"{size:.2f}",
            order_id=str(order_id)[:20],
            status=status,
            success=success,
            making_amount=making_amount,
            taking_amount=taking_amount,
            size_matched=size_matched,
            filled=filled,
            error_msg=error_msg if error_msg else None,
        )

        return {
            "filled": filled,
            "size_matched": size_matched,
            "order_id": str(order_id),
            "making_amount": making_amount,
            "taking_amount": taking_amount,
            "status": status,
        }

    async def place_market_order(
        self,
        token_id: str,
        price: float,
        size: float,
        order_type: str = "FAK",
    ) -> dict:
        """Submit a market order with configurable type (FAK/FOK).

        This is the v9.0 unified entry point used by the price ladder.
        Delegates to place_fok_order() with the appropriate OrderType.

        Args:
            token_id: CLOB outcome token ID.
            price: Worst-price limit (slippage cap).
            size: Number of shares to buy.
            order_type: "FAK" (Fill-And-Kill) or "FOK" (Fill-Or-Kill).

        Returns:
            dict with keys: filled (bool), size_matched (float), order_id (str).
        """
        # FAK and FOK share the same submission logic — only OrderType differs.
        # The py-clob-client SDK supports both via OrderType enum.
        if self.paper_mode:
            # Paper mode: simulate fill at requested price
            import math

            _sim_size = math.floor(size * 100) / 100
            return {
                "filled": True,
                "size_matched": _sim_size,
                "order_id": f"paper-{order_type.lower()}-{uuid.uuid4().hex[:8]}",
            }

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        stake_usd = price * size
        if stake_usd > LIVE_MAX_TRADE_USD:
            raise ValueError(
                f"Trade stake ${stake_usd:.2f} exceeds cap ${LIVE_MAX_TRADE_USD:.2f}"
            )

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        client = self._clob_client

        import math

        _price = round(price, 4)
        _size = math.floor(size * 100) / 100
        for _adj in range(100):
            _maker = round(_price * _size, 6)
            if abs(_maker - round(_maker, 2)) < 1e-9:
                break
            _size -= 0.01
        _size = max(_size, 0.01)
        if _size <= 0:
            return {"filled": False, "size_matched": 0, "order_id": None}

        order_args = OrderArgs(
            token_id=token_id,
            price=float(f"{_price:.4f}"),
            size=float(f"{_size:.2f}"),
            side=BUY,
        )

        # Select order type from SDK enum
        ot = order_type.upper()
        if ot == "FAK":
            sdk_type = OrderType.FAK
        elif ot == "FOK":
            sdk_type = OrderType.FOK
        else:
            self._log.warning(
                "place_market_order.unknown_order_type", requested=ot, fallback="FOK"
            )
            sdk_type = OrderType.FOK

        def _sign_and_submit():
            signed = client.create_order(order_args)
            return client.post_order(signed, sdk_type)

        self._log.info(
            "place_market_order.submitting",
            order_type=ot,
            token_id=token_id[:20] + "...",
            price=f"${price:.4f}",
            size=f"{size:.2f}",
        )

        response = await asyncio.to_thread(_sign_and_submit)

        # CRITICAL BUG FIX (Apr 10): See place_fok_order for full context.
        # Polymarket returns makingAmount/takingAmount (not size_matched) and
        # lowercase status ("matched"/"unmatched"/"live"). The old parsing was
        # silently failing on every fill and causing the ladder to over-buy.
        def _get(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        order_id = (
            _get(response, "orderID")
            or _get(response, "id")
            or f"{ot.lower()}-{uuid.uuid4().hex[:12]}"
        )
        status_raw = _get(response, "status", "UNKNOWN") or "UNKNOWN"
        status = str(status_raw).lower()
        success = bool(_get(response, "success", False))
        error_msg = _get(response, "errorMsg", "") or ""
        making_raw = _get(response, "makingAmount", "0")
        taking_raw = _get(response, "takingAmount", "0")

        try:
            making_amount = float(making_raw) if making_raw else 0.0
        except (ValueError, TypeError):
            making_amount = 0.0
        try:
            taking_amount = float(taking_raw) if taking_raw else 0.0
        except (ValueError, TypeError):
            taking_amount = 0.0

        # BUY: makingAmount=USDC paid, takingAmount=shares received
        size_matched = taking_amount
        filled = (status == "matched") or (size_matched > 0)

        self._log.info(
            "place_market_order.result",
            order_type=ot,
            filled=filled,
            size_matched=size_matched,
            making_amount=making_amount,
            taking_amount=taking_amount,
            order_id=str(order_id)[:20],
            status=status,
            success=success,
            error_msg=error_msg if error_msg else None,
        )

        return {
            "filled": filled,
            "size_matched": size_matched,
            "order_id": str(order_id),
            "making_amount": making_amount,
            "taking_amount": taking_amount,
            "status": status,
        }

    async def get_order_book_spread(self, token_id: str) -> float:
        """Get the best ask - best bid spread for a token.

        Returns spread in price units (e.g. 0.02 = 2¢ spread).
        Returns 0.02 as default if book can't be read.
        """
        if self.paper_mode or not self._clob_client:
            return 0.02  # Default 2¢ spread assumption

        try:
            client = self._clob_client
            book = await asyncio.to_thread(client.get_order_book, token_id)

            best_bid = float(book.bids[0].price) if book.bids else 0.0
            best_ask = float(book.asks[0].price) if book.asks else 1.0
            spread = best_ask - best_bid

            self._log.debug(
                "order_book.spread",
                token_id=token_id[:20] + "...",
                best_bid=f"${best_bid:.4f}",
                best_ask=f"${best_ask:.4f}",
                spread=f"${spread:.4f}",
            )
            return max(0.005, spread)  # Floor at 0.5¢
        except Exception as exc:
            self._log.debug("order_book.spread_error", error=str(exc))
            return 0.02  # Default fallback

    def calculate_dynamic_bump(self, base_price: float, spread: float) -> float:
        """Calculate dynamic price bump based on order book spread.

        Strategy: bump by min(2¢, spread) — don't overpay more than the spread,
        but always bump at least 0.5¢ to cross the spread.

        Args:
            base_price: Original order price
            spread: Current bid-ask spread from get_order_book_spread()

        Returns:
            Bumped price (capped at 65¢ for 5m, safety enforced by caller)
        """
        bump = min(0.02, max(0.005, spread))
        bumped = round(base_price + bump, 4)
        self._log.info(
            "price.dynamic_bump",
            base=f"${base_price:.4f}",
            spread=f"${spread:.4f}",
            bump=f"${bump:.4f}",
            bumped=f"${bumped:.4f}",
        )
        return bumped

    # ------------------------------------------------------------------
    # Low-level CLOB call
    # ------------------------------------------------------------------

    async def place_rfq_order(
        self,
        token_id: str,
        direction: str,
        price: float,
        size: float,
        max_price: float = 0.65,
    ) -> tuple[str | None, float | None]:
        """
        Place an order via RFQ (Request for Quote) system.

        UpDown tokens have NO CLOB order book — market makers respond to RFQ only.

        Flow:
        1. Create RFQ request at our target price
        2. Check for best quote from market makers
        3. If quote price <= max_price, accept it
        4. Return (order_id, fill_price) or (None, None)

        Args:
            token_id: The conditional token ID
            direction: "YES" or "NO"
            price: Our target price
            size: Number of shares to buy
            max_price: Maximum acceptable price (cap)

        Returns:
            (order_id, fill_price) if filled, (None, None) if not
        """
        if self.paper_mode:
            self._log.info("rfq.paper_mode", price=f"${price:.4f}", size=f"{size:.1f}")
            return (f"rfq-paper-{__import__('uuid').uuid4().hex[:12]}", price)

        if not self._clob_client:
            self._log.error("rfq.no_clob_client")
            return (None, None)

        try:
            from py_clob_client.rfq import RfqUserRequest
            from py_clob_client.order_builder.constants import BUY

            side = BUY  # We always BUY tokens

            self._log.info(
                "rfq.creating_request",
                token_id=token_id[:20] + "...",
                direction=direction,
                price=f"${price:.4f}",
                size=f"{size:.1f}",
                max_price=f"${max_price:.2f}",
            )

            # Create the RFQ request
            user_request = RfqUserRequest(
                token_id=token_id,
                price=price,
                side=side,
                size=size,
            )

            response = await asyncio.to_thread(
                self._clob_client.rfq.create_rfq_request,
                user_request,
            )

            request_id = (
                response.get("requestId")
                or response.get("request_id")
                or response.get("id")
            )
            if not request_id:
                self._log.warning("rfq.no_request_id", response=str(response)[:200])
                return (None, None)

            self._log.info("rfq.request_created", request_id=str(request_id)[:20])

            # Wait a moment for market makers to respond
            await asyncio.sleep(2)

            # Get best quote
            from py_clob_client.rfq import GetRfqBestQuoteParams

            best_quote = await asyncio.to_thread(
                self._clob_client.rfq.get_rfq_best_quote,
                request_id,
            )

            if not best_quote:
                self._log.info("rfq.no_quotes", request_id=str(request_id)[:20])
                # Cancel the request
                try:
                    from py_clob_client.rfq import CancelRfqRequestParams

                    await asyncio.to_thread(
                        self._clob_client.rfq.cancel_rfq_request,
                        request_id,
                    )
                except Exception:
                    pass
                return (None, None)

            # Check quote price
            quote_price = float(best_quote.get("price", 0))
            quote_id = (
                best_quote.get("quoteId")
                or best_quote.get("quote_id")
                or best_quote.get("id")
            )

            self._log.info(
                "rfq.quote_received",
                quote_price=f"${quote_price:.4f}",
                max_price=f"${max_price:.2f}",
                quote_id=str(quote_id)[:20] if quote_id else "none",
            )

            if quote_price > max_price:
                self._log.warning(
                    "rfq.quote_too_expensive",
                    quote_price=f"${quote_price:.4f}",
                    max_price=f"${max_price:.2f}",
                )
                try:
                    await asyncio.to_thread(
                        self._clob_client.rfq.cancel_rfq_request,
                        request_id,
                    )
                except Exception:
                    pass
                return (None, None)

            # Accept the quote
            result = await asyncio.to_thread(
                self._clob_client.rfq.accept_rfq_quote,
                quote_id,
            )

            order_id = (
                result.get("orderID")
                or result.get("order_id")
                or result.get("id")
                or str(quote_id)
            )

            self._log.info(
                "rfq.accepted",
                order_id=str(order_id)[:20] if order_id else "none",
                fill_price=f"${quote_price:.4f}",
            )

            return (str(order_id), quote_price)

        except Exception as exc:
            self._log.warning("rfq.failed", error=str(exc)[:200])
            return (None, None)

    # place_market_order_legacy DELETED in v10 cleanup (was duplicate of place_market_order)

    def get_current_market_slug(self) -> str:
        """Return the slug for the current 5-minute BTC up/down window.

        The window timestamp is the floor of the current Unix time to the
        nearest 300 seconds, giving a stable slug for the active window.
        """
        now = int(time.time())
        window_ts = now - (now % POLY_WINDOW_SECONDS)
        return f"btc-updown-5m-{window_ts}"

    async def get_market_prices(self, market_slug: str) -> dict[str, Decimal]:
        """Fetch best-bid prices for YES and NO outcomes.

        Args:
            market_slug: Market identifier.

        Returns:
            {"yes": Decimal, "no": Decimal} where values sum to ~1.
        """
        if self.paper_mode:
            # Simulate a slightly noisy 50/50 market
            yes_price = Decimal(str(round(random.uniform(0.46, 0.54), 4)))
            no_price = Decimal("1.0000") - yes_price
            self._log.debug(
                "get_market_prices.paper",
                market_slug=market_slug,
                yes=str(yes_price),
                no=str(no_price),
            )
            return {"yes": yes_price, "no": no_price}

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        # Resolve market slug to a token ID via the CLOB search endpoint,
        # then fetch the order book for the YES token.
        # Uses asyncio.to_thread() because py-clob-client is synchronous.
        client = self._clob_client
        try:

            def _fetch_prices():
                # Search for specific market instead of fetching all markets
                # py-clob-client get_market(condition_id) or get_markets() with next_cursor
                # Fallback: iterate paginated results with a slug filter
                import httpx as _httpx

                resp = _httpx.get(
                    "https://clob.polymarket.com/markets",
                    params={"slug": market_slug},
                    timeout=10,
                )
                resp.raise_for_status()
                markets = resp.json()

                if isinstance(markets, dict):
                    # Paginated response: {"data": [...], "next_cursor": ...}
                    markets = markets.get("data", [])

                yes_token_id = None
                for market in markets or []:
                    slug = market.get("market_slug", "") or market.get("slug", "")
                    if market_slug in slug:
                        tokens = market.get("clobTokenIds") or market.get("tokens", [])
                        if tokens and len(tokens) >= 1:
                            yes_token_id = (
                                tokens[0]
                                if isinstance(tokens[0], str)
                                else tokens[0].get("token_id")
                            )
                        break

                if not yes_token_id:
                    return None

                order_book = client.get_order_book(yes_token_id)
                if order_book and order_book.asks:
                    return Decimal(str(order_book.asks[0].price))
                return Decimal("0.5")

            yes_best_ask = await asyncio.to_thread(_fetch_prices)

            if yes_best_ask is None:
                self._log.warning(
                    "get_market_prices.token_not_found", market_slug=market_slug
                )
                return {}

            no_price = Decimal("1") - yes_best_ask

            self._log.debug(
                "get_market_prices.live",
                market_slug=market_slug,
                yes=str(yes_best_ask),
                no=str(no_price),
            )
            return {"yes": yes_best_ask, "no": no_price}

        except Exception as exc:
            self._log.error("get_market_prices.live_error", error=str(exc))
            return {}

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        """Return current USD balance.

        Paper mode tracks a simulated balance that decreases with each order.
        """
        if self.paper_mode:
            self._log.debug("get_balance.paper", balance=self._paper_balance)
            return self._paper_balance

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        from py_clob_client.clob_types import BalanceAllowanceParams

        sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "2"))

        def _fetch():
            params = BalanceAllowanceParams(
                asset_type="COLLATERAL", signature_type=sig_type
            )
            ba = self._clob_client.get_balance_allowance(params)
            return int(ba.get("balance", "0")) / 1e6

        return float(await asyncio.to_thread(_fetch))

    async def get_order_status(self, order_id: str) -> dict:
        """Return status dict for a given order ID.

        Args:
            order_id: The order ID returned from place_order().

        Returns:
            Dict with keys: order_id, status, fill_price, stake_usd, etc.
        """
        if self.paper_mode:
            if order_id not in self._paper_orders:
                return {"order_id": order_id, "status": "NOT_FOUND"}
            return dict(self._paper_orders[order_id])

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        resp = await asyncio.to_thread(self._clob_client.get_order, order_id)

        # v11 fix: Normalize status to UPPERCASE for back-compat with
        # fill_check loops that compare `clob_status not in ("LIVE","UNKNOWN")`.
        # Polymarket returns lowercase: 'live'/'matched'/'unmatched'/'delayed'.
        # Also try multiple field names for size_matched since we've seen
        # both size_matched and sizeMatched across CLOB API versions.
        def _get(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        raw_status = _get(resp, "status", "UNKNOWN") or "UNKNOWN"
        status = str(raw_status).upper()

        size_matched = None
        for key in ("size_matched", "sizeMatched", "takingAmount"):
            val = _get(resp, key)
            if val is not None and val != "":
                try:
                    size_matched = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        price = _get(resp, "price")

        return {
            "order_id": order_id,
            "status": status,
            "size_matched": size_matched if size_matched is not None else 0,
            "price": price,
            "raw": resp if isinstance(resp, dict) else None,
        }

    async def get_portfolio_value(self) -> float:
        """Return total portfolio value: CLOB cash + open position value.

        Fetches position value from Polymarket data API so unredeemed
        wins are included in the bankroll calculation.
        """
        cash = await self.get_balance()

        if self.paper_mode:
            return cash

        # Fetch position value from data API
        try:
            import aiohttp

            funder = self._funder_address.lower()
            url = f"https://data-api.polymarket.com/positions?user={funder}"
            headers = {"User-Agent": "Mozilla/5.0 NovakashEngine/1.0"}

            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return cash
                    positions = await resp.json()

            position_value = 0.0
            for p in positions:
                sz = float(p.get("size", 0))
                cur = float(p.get("curPrice", 0))
                position_value += sz * cur

            total = cash + position_value
            self._log.debug(
                "portfolio.value",
                cash=f"${cash:.2f}",
                positions=f"${position_value:.2f}",
                total=f"${total:.2f}",
            )
            return total
        except Exception as exc:
            self._log.debug("portfolio.position_fetch_failed", error=str(exc))
            return cash  # Fallback to cash only

    async def get_position_outcomes(self) -> dict:
        """Fetch real position outcomes from Polymarket data API.

        Returns dict of conditionId → {size, avgPrice, curPrice, outcome}
        where outcome is 'WIN' (curPrice >= 0.99), 'LOSS' (curPrice <= 0.01),
        or 'OPEN' (still trading).

        This is the SOURCE OF TRUTH for trade resolution — NOT internal logic.
        """
        if self.paper_mode:
            return {}

        try:
            import aiohttp

            funder = self._funder_address.lower()
            url = f"https://data-api.polymarket.com/positions?user={funder}"
            headers = {"User-Agent": "Mozilla/5.0 NovakashEngine/1.0"}

            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return {}
                    positions = await resp.json()

            results = {}
            for p in positions:
                cid = p.get("conditionId", "")
                cur_price = float(p.get("curPrice", 0))
                size = float(p.get("size", 0))
                avg_price = float(p.get("avgPrice", 0))

                if cur_price <= 0.01:
                    outcome = "LOSS"
                elif cur_price >= 0.99:
                    outcome = "WIN"
                else:
                    outcome = "OPEN"

                results[cid] = {
                    "size": size,
                    "avgPrice": avg_price,
                    "curPrice": cur_price,
                    "outcome": outcome,
                    "value": size * cur_price,
                    "cost": size * avg_price,
                    "pnl": (size * cur_price) - (size * avg_price),
                    "tokenId": p.get("asset", "") or p.get("tokenId", ""),
                    "asset": p.get("asset", ""),
                }

            return results
        except Exception as exc:
            self._log.debug("positions.fetch_failed", error=str(exc))
            return {}

    async def get_open_orders(self) -> list[dict]:
        """Fetch all open/resting orders from the CLOB.

        Paper mode returns all tracked paper orders.
        Live mode queries the CLOB client for active orders.

        Returns:
            List of order dicts with keys: id, asset_id, price, size, size_matched, status.
        """
        if self.paper_mode:
            return list(self._paper_orders.values())

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        def _fetch():
            return self._clob_client.get_orders()

        raw = await asyncio.to_thread(_fetch)
        # Normalise: py-clob-client may return a dict with 'data' key or a list
        if isinstance(raw, dict):
            return raw.get("data", [])
        if isinstance(raw, list):
            return raw
        return []

    async def get_trade_history(self) -> list[dict]:
        """Fetch filled trade history from the CLOB.

        Each fill dict includes asset_id (token_id), outcome, side, price,
        size, match_time, and status.  Used by the reconciler to detect
        orphaned GTC fills that the engine missed during its polling window.

        Paper mode returns an empty list (no real fills to fetch).

        Returns:
            List of fill dicts from the CLOB trade history API.
        """
        if self.paper_mode:
            return []

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        def _fetch():
            return self._clob_client.get_trades()

        raw = await asyncio.to_thread(_fetch)

        # Normalise: API may return dict with 'data' key or a list directly
        if isinstance(raw, dict):
            return raw.get("data", [])
        if isinstance(raw, list):
            return raw
        return []

    # ─── POLY-SOT methods ───────────────────────────────────────────────────
    #
    # Two new entry points the SOT reconciler uses to verify that the engine's
    # local manual_trades rows match what Polymarket actually booked. They
    # share parsing helpers but expose typed `OrderStatus` results so the
    # reconciler logic stays free of CLOB wire-format quirks.

    @staticmethod
    def _normalise_status(raw_status: Any) -> str:
        """Map Polymarket's status string to the SOT alphabet.

        Polymarket has been seen returning (lowercase): 'live', 'matched',
        'unmatched', 'delayed', 'filled', 'cancelled', 'canceled', 'rejected'.
        Some upstream wrappers also UPPERCASE. We collapse to:
            pending | matched | filled | cancelled | rejected | unknown
        """
        if raw_status is None:
            return "unknown"
        s = str(raw_status).strip().lower()
        if not s or s == "unknown":
            return "unknown"
        if s == "matched":
            return "matched"
        if s == "filled":
            return "filled"
        if s in ("live", "delayed", "pending", "open"):
            return "pending"
        if s in ("cancelled", "canceled", "expired"):
            return "cancelled"
        if s in ("rejected", "unmatched", "failed"):
            return "rejected"
        return "unknown"

    @classmethod
    def _parse_order_status(cls, order_id: str, resp: Any) -> PolyOrderStatus:
        """Coerce a raw CLOB get_order response into a typed OrderStatus."""

        def _get(obj, key, default=None):
            if obj is None:
                return default
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        status = cls._normalise_status(_get(resp, "status", "unknown"))

        # Fill size: try every key Polymarket has been seen using
        fill_size: Optional[float] = None
        for key in ("size_matched", "sizeMatched", "takingAmount", "filled_size"):
            val = _get(resp, key)
            if val is not None and val != "":
                try:
                    fill_size = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        # Fill price: average price for the executed portion. Polymarket
        # returns the limit `price` for resting orders and `avgFillPrice` /
        # `average_price` for matched ones — try them all.
        fill_price: Optional[float] = None
        for key in ("avgFillPrice", "average_price", "fill_price", "price"):
            val = _get(resp, key)
            if val is not None and val != "":
                try:
                    fill_price = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        # Timestamp of last status change. CLOB returns either match_time,
        # updated_at, or created_at depending on the lifecycle.
        ts: Optional[datetime] = None
        for key in (
            "match_time",
            "matchTime",
            "updated_at",
            "updatedAt",
            "created_at",
            "createdAt",
        ):
            val = _get(resp, key)
            if val is None:
                continue
            try:
                if isinstance(val, (int, float)):
                    # epoch seconds
                    ts = datetime.fromtimestamp(float(val), tz=timezone.utc)
                else:
                    # ISO string — try to parse with fromisoformat
                    s = str(val).replace("Z", "+00:00")
                    ts = datetime.fromisoformat(s)
                break
            except (ValueError, TypeError, OSError):
                continue

        return PolyOrderStatus(
            order_id=order_id,
            status=status,
            fill_price=fill_price,
            fill_size=fill_size,
            timestamp=ts,
            raw=resp if isinstance(resp, dict) else None,
        )

    async def get_order_status_sot(self, order_id: str) -> Optional[PolyOrderStatus]:
        """SOT-grade order status fetch — returns None if Polymarket has no record.

        Distinct from the legacy ``get_order_status`` (which returns a dict
        with status='NOT_FOUND' for missing orders) so the reconciler can
        cleanly distinguish:
          * None             → Polymarket has no order with that ID
          * OrderStatus(...) → Polymarket has the order, status is reliable

        Paper mode returns a synthetic OrderStatus from the local
        ``_paper_orders`` dict; this lets the SOT reconciler exercise the
        full code path in paper mode without hitting the live CLOB.

        Errors talking to the SDK are NOT swallowed — they propagate so the
        reconciler can record them as `unreconciled` and retry on the next
        pass instead of marking a row `engine_optimistic` based on a
        transient network blip.
        """
        if not order_id:
            return None

        if self.paper_mode:
            paper = self._paper_orders.get(order_id)
            if paper is None:
                # In paper mode, manual trades use synthetic IDs like
                # `manual-paper-...` that aren't in _paper_orders. Treat them
                # as "filled at requested price" so the SOT reconciler stamps
                # them `agrees` instead of false-positive `engine_optimistic`.
                if order_id.startswith("manual-paper-"):
                    return PolyOrderStatus(
                        order_id=order_id,
                        status="filled",
                        fill_price=None,
                        fill_size=None,
                        timestamp=datetime.now(timezone.utc),
                        raw={"synthetic_paper": True},
                    )
                return None
            try:
                fill_price = (
                    float(paper.get("fill_price"))
                    if paper.get("fill_price") is not None
                    else None
                )
            except (ValueError, TypeError):
                fill_price = None
            try:
                fill_size = (
                    float(paper.get("shares"))
                    if paper.get("shares") is not None
                    else None
                )
            except (ValueError, TypeError):
                fill_size = None
            ts: Optional[datetime] = None
            try:
                if paper.get("filled_at"):
                    ts = datetime.fromtimestamp(
                        float(paper["filled_at"]), tz=timezone.utc
                    )
            except (ValueError, TypeError, OSError):
                ts = None
            return PolyOrderStatus(
                order_id=order_id,
                status="filled"
                if str(paper.get("status", "")).upper() == "FILLED"
                else "pending",
                fill_price=fill_price,
                fill_size=fill_size,
                timestamp=ts,
                raw=paper,
            )

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        def _fetch():
            return self._clob_client.get_order(order_id)

        try:
            resp = await asyncio.to_thread(_fetch)
        except Exception as exc:
            # Check for "not found" responses — these are NOT errors, they're
            # the SOT-defining signal that the order never made it to the CLOB.
            msg = str(exc).lower()
            if "404" in msg or "not found" in msg or "not_found" in msg:
                self._log.info(
                    "get_order_status_sot.not_found",
                    order_id=order_id[:20],
                )
                return None
            # Anything else is a real error — let the reconciler decide.
            raise

        if resp is None:
            return None

        return self._parse_order_status(order_id, resp)

    async def list_recent_orders(
        self,
        since: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[PolyOrderStatus]:
        """Bulk SOT helper — return parsed PolyOrderStatus for recent CLOB orders.

        The reconciler's primary mode is per-order lookup via
        ``get_order_status_sot``, but this bulk pass is useful when the
        engine needs to detect ``polymarket_only`` rows (orders Polymarket
        booked that the engine has no manual_trades record for).

        Paper mode returns parsed entries from the local ``_paper_orders``
        dict, optionally filtered by ``since``.
        """
        if self.paper_mode:
            results: list[PolyOrderStatus] = []
            for oid, p in self._paper_orders.items():
                ts: Optional[datetime] = None
                try:
                    if p.get("created_at"):
                        ts = datetime.fromtimestamp(
                            float(p["created_at"]), tz=timezone.utc
                        )
                except (ValueError, TypeError, OSError):
                    ts = None
                if since is not None and ts is not None and ts < since:
                    continue
                try:
                    fp = (
                        float(p.get("fill_price"))
                        if p.get("fill_price") is not None
                        else None
                    )
                except (ValueError, TypeError):
                    fp = None
                try:
                    fs = float(p.get("shares")) if p.get("shares") is not None else None
                except (ValueError, TypeError):
                    fs = None
                results.append(
                    PolyOrderStatus(
                        order_id=oid,
                        status="filled"
                        if str(p.get("status", "")).upper() == "FILLED"
                        else "pending",
                        fill_price=fp,
                        fill_size=fs,
                        timestamp=ts,
                        raw=p,
                    )
                )
                if len(results) >= limit:
                    break
            return results

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        def _fetch():
            return self._clob_client.get_orders()

        try:
            raw = await asyncio.to_thread(_fetch)
        except Exception as exc:
            self._log.warning("list_recent_orders.fetch_failed", error=str(exc)[:200])
            return []

        if isinstance(raw, dict):
            raw = raw.get("data", [])
        if not isinstance(raw, list):
            return []

        results: list[PolyOrderStatus] = []
        for entry in raw[: int(limit)]:
            oid = None
            if isinstance(entry, dict):
                oid = entry.get("orderID") or entry.get("id") or entry.get("order_id")
            else:
                oid = getattr(entry, "orderID", None) or getattr(entry, "id", None)
            if not oid:
                continue
            parsed = self._parse_order_status(str(oid), entry)
            if (
                since is not None
                and parsed.timestamp is not None
                and parsed.timestamp < since
            ):
                continue
            results.append(parsed)
        return results
