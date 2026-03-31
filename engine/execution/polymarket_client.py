"""
Polymarket CLOB client — paper mode + live mode stub.

Paper mode simulates order fills with realistic slippage.
Live mode stubs are clearly marked with TODO comments.
"""

from __future__ import annotations

import random
import time
import uuid
from decimal import Decimal
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

# 5-minute prediction windows
POLY_WINDOW_SECONDS = 300

# Simulated paper balance (USD)
_PAPER_BALANCE_USD = 10_000.0


class PolymarketClient:
    """
    Client for interacting with the Polymarket CLOB API.

    In paper mode all orders are simulated locally with realistic slippage.
    In live mode the CLOB REST/WS API is used (stubs marked TODO).

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

        self._log = logger.bind(component="polymarket_client", paper_mode=paper_mode)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Initialise the CLOB client connection.

        Paper mode just logs the startup.
        Live mode would construct the py-clob-client and authenticate.
        """
        if self.paper_mode:
            self._log.info("polymarket_client.connected", mode="paper")
            return

        # TODO: Initialise live CLOB client
        # from py_clob_client.client import ClobClient
        # self._clob_client = ClobClient(
        #     host="https://clob.polymarket.com",
        #     key=self._private_key,
        #     chain_id=137,  # Polygon mainnet
        #     creds=ApiCreds(
        #         api_key=self._api_key,
        #         api_secret=self._api_secret,
        #         api_passphrase=self._api_passphrase,
        #     ),
        #     signature_type=2,  # POLY_GNOSIS_SAFE
        #     funder=self._funder_address,
        # )
        # self._clob_client.set_api_creds(self._clob_client.create_or_derive_api_creds())
        raise NotImplementedError("Live Polymarket connection not yet implemented")

    # ------------------------------------------------------------------
    # High-level convenience — called by strategies
    # ------------------------------------------------------------------

    async def place_order(
        self,
        market_slug: str,
        direction: str,
        price: Decimal,
        stake_usd: float,
    ) -> str:
        """Place a directional order on a binary market.

        Args:
            market_slug: Human-readable market identifier, e.g.
                         "btc-updown-5m-1711900800".
            direction: "YES" or "NO".
            price: Expected fill price in [0, 1].
            stake_usd: Notional USD amount to risk.

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

        # TODO: Live order placement
        # 1. Resolve market_slug → token_id via get_markets()
        # 2. Determine CLOB side: YES → BUY, NO → SELL (or BUY the NO token)
        # 3. Call place_market_order()
        raise NotImplementedError("Live order placement not yet implemented")

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

        # TODO: Live CLOB order
        # order = MarketOrderArgs(token_id=token_id, amount=amount_usd)
        # resp = self._clob_client.create_and_post_order(order)
        # return resp["orderID"]
        raise NotImplementedError("Live place_market_order not yet implemented")

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

        # TODO: Live price fetch
        # order_book = self._clob_client.get_order_book(token_id_yes)
        # yes_best_ask = Decimal(order_book.asks[0].price) if order_book.asks else Decimal("0.5")
        # return {"yes": yes_best_ask, "no": Decimal("1") - yes_best_ask}
        raise NotImplementedError("Live get_market_prices not yet implemented")

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

        # TODO: Live balance
        # return float(self._clob_client.get_balance())
        raise NotImplementedError("Live get_balance not yet implemented")

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

        # TODO: Live order status
        # resp = self._clob_client.get_order(order_id)
        # return {"order_id": order_id, "status": resp["status"], ...}
        raise NotImplementedError("Live get_order_status not yet implemented")
