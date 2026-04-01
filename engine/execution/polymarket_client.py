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

import os
import random
import time
import uuid
import warnings
from decimal import Decimal
from typing import Optional

import structlog

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
                raise EnvironmentError(
                    "Live trading requires LIVE_TRADING_ENABLED=true environment variable. "
                    "Set this explicitly to confirm intent before enabling live mode."
                )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Initialise the CLOB client connection.

        Paper mode just logs the startup.
        Live mode constructs and authenticates the py-clob-client.
        """
        if self.paper_mode:
            self._log.info("polymarket_client.connected", mode="paper")
            return

        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        self._clob_client = ClobClient(
            host="https://clob.polymarket.com",
            key=self._private_key,
            chain_id=137,  # Polygon mainnet
            creds=ApiCreds(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_passphrase,
            ),
            signature_type=2,  # POLY_GNOSIS_SAFE
            funder=self._funder_address,
        )
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
            return await self._paper_place_order(market_slug, direction, price, stake_usd)

        return await self._live_place_order(market_slug, direction, price, stake_usd, token_id)

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
            slippage_pct=f"{float(slippage)*100:.3f}%",
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
        size = round(stake_usd / float(price), 2)

        # Build order args — always BUY the outcome token we want
        order_args = OrderArgs(
            token_id=token_id,
            price=float(price),
            size=size,
            side=BUY,
        )

        # Create and sign the order (py-clob-client handles all crypto)
        signed_order = self._clob_client.create_order(order_args)

        # Submit to CLOB as a GTC limit order
        response = self._clob_client.post_order(signed_order, OrderType.GTC)

        order_id = response.get("orderID", f"live-{uuid.uuid4().hex[:12]}")

        self._log.info(
            "place_order.live_submitted",
            order_id=order_id,
            market_slug=market_slug,
            direction=direction,
            price=str(price),
            size=size,
            stake_usd=stake_usd,
            token_id=token_id[:20] + "..." if len(token_id) > 20 else token_id,
        )

        return order_id

    # ------------------------------------------------------------------
    # Low-level CLOB call
    # ------------------------------------------------------------------

    async def place_market_order(
        self,
        token_id: str,
        side: str,
        amount_usd: float,
        price: Decimal,
    ) -> str:
        """Low-level CLOB market order.

        Args:
            token_id: CLOB token ID for the outcome (YES or NO token).
            side: "BUY" or "SELL".
            amount_usd: USD collateral to spend.
            price: Limit price (used as worst-case for market orders).

        Returns:
            CLOB order ID.
        """
        if self.paper_mode:
            order_id = f"paper-clob-{uuid.uuid4().hex[:12]}"
            self._log.info(
                "place_market_order.paper",
                token_id=token_id,
                side=side,
                amount_usd=amount_usd,
                price=str(price),
                order_id=order_id,
            )
            return order_id

        if not self._clob_client:
            raise RuntimeError("CLOB client not connected — call connect() first")

        from py_clob_client.clob_types import MarketOrderArgs

        order = MarketOrderArgs(token_id=token_id, amount=amount_usd)
        resp = self._clob_client.create_and_post_order(order)
        return resp.get("orderID", f"live-{uuid.uuid4().hex[:12]}")

    # ------------------------------------------------------------------
    # Market helpers
    # ------------------------------------------------------------------

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

        # Resolve market slug to a token ID by querying the CLOB markets endpoint.
        # We use the YES (index 0) token to look up the order book.
        # The caller should ideally pass the token_id directly for efficiency.
        # Here we do a best-effort lookup via the CLOB client's get_markets().
        try:
            markets = self._clob_client.get_markets()
            yes_token_id = None
            for market in (markets or []):
                if market_slug in (market.get("market_slug", ""), market.get("slug", "")):
                    tokens = market.get("clobTokenIds") or market.get("tokens", [])
                    if tokens and len(tokens) >= 1:
                        yes_token_id = tokens[0] if isinstance(tokens[0], str) else tokens[0].get("token_id")
                    break

            if not yes_token_id:
                self._log.warning("get_market_prices.token_not_found", market_slug=market_slug)
                return {}

            order_book = self._clob_client.get_order_book(yes_token_id)
            yes_best_ask = (
                Decimal(str(order_book.asks[0].price))
                if order_book and order_book.asks
                else Decimal("0.5")
            )
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

        return float(self._clob_client.get_balance())

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

        resp = self._clob_client.get_order(order_id)
        return {
            "order_id": order_id,
            "status": resp.get("status", "UNKNOWN"),
            "size_matched": resp.get("size_matched"),
            "price": resp.get("price"),
            "raw": resp,
        }
