"""
Opinion Markets client — paper mode + live mode stub.

Opinion is a crypto prediction market. Fee model:
    fee = OPINION_CRYPTO_FEE_MULT * price * (1 - price)

Paper mode simulates all bets locally with realistic fill behaviour.
"""

from __future__ import annotations

import random
import time
import uuid
from decimal import Decimal
from typing import Optional

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

# Opinion fee multiplier applied to quadratic price term
OPINION_CRYPTO_FEE_MULT: float = 0.02

# Simulated paper balance
_PAPER_BALANCE_USD = 10_000.0

# Opinion REST base URL (placeholder — update when API is live)
OPINION_API_BASE = "https://api.opinion.markets/v1"


class OpinionClient:
    """
    Client for placing bets on Opinion prediction markets.

    Args:
        api_key: Opinion API key.
        wallet_key: Private key / wallet secret for signing transactions.
        paper_mode: If True (default), simulate all trades locally.
    """

    def __init__(
        self,
        api_key: str,
        wallet_key: str,
        paper_mode: bool = True,
    ) -> None:
        self._api_key = api_key
        self._wallet_key = wallet_key
        self.paper_mode = paper_mode

        self._connected: bool = False
        self._session: Optional[aiohttp.ClientSession] = None

        # Paper-mode state
        self._paper_balance: float = _PAPER_BALANCE_USD
        self._paper_bets: dict[str, dict] = {}

        self._log = logger.bind(component="opinion_client", paper_mode=paper_mode)

    # ------------------------------------------------------------------
    # Connection property
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True when the client is connected and ready to place orders."""
        return self._connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Initialise HTTP session and authenticate.

        Paper mode marks as connected and logs.
        Live mode would validate API credentials.
        """
        if self.paper_mode:
            self._connected = True
            self._log.info("opinion_client.connected", mode="paper")
            return

        # Create shared aiohttp session for live calls
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
        )

        # TODO: Validate credentials with a ping / auth endpoint
        # async with self._session.get(f"{OPINION_API_BASE}/me") as resp:
        #     resp.raise_for_status()
        #     data = await resp.json()
        #     self._log.info("opinion_client.connected", user=data.get("username"))
        self._connected = True
        self._log.info("opinion_client.connected", mode="live")

    async def disconnect(self) -> None:
        """Close HTTP session and mark as disconnected."""
        self._connected = False
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._log.info("opinion_client.disconnected")

    # ------------------------------------------------------------------
    # High-level convenience — called by strategies
    # ------------------------------------------------------------------

    async def place_order(
        self,
        market_id: str,
        direction: str,
        price: Decimal,
        stake_usd: float,
    ) -> str:
        """Convenience method called directly by strategies.

        Computes the platform fee and delegates to place_bet().

        Fee model:
            fee = OPINION_CRYPTO_FEE_MULT * price * (1 - price)

        Args:
            market_id: Opinion market identifier.
            direction: "YES" or "NO".
            price: Expected fill price in (0, 1).
            stake_usd: Notional USD to risk (before fee deduction).

        Returns:
            Bet ID string.
        """
        direction = direction.upper()
        if direction not in {"YES", "NO"}:
            raise ValueError(f"direction must be YES or NO, got {direction!r}")

        fee = float(OPINION_CRYPTO_FEE_MULT) * float(price) * (1.0 - float(price))
        effective_stake = stake_usd * (1.0 - fee)

        self._log.info(
            "place_order.requested",
            market_id=market_id,
            direction=direction,
            price=str(price),
            stake_usd=stake_usd,
            fee_pct=f"{fee*100:.3f}%",
            effective_stake=effective_stake,
        )

        return await self.place_bet(
            market_id=market_id,
            direction=direction,
            stake_usd=effective_stake,
            expected_price=price,
        )

    # ------------------------------------------------------------------
    # Core bet placement
    # ------------------------------------------------------------------

    async def place_bet(
        self,
        market_id: str,
        direction: str,
        stake_usd: float,
        expected_price: Decimal,
        slippage_pct: float = 0.02,
    ) -> str:
        """Place a bet with slippage protection.

        Args:
            market_id: Opinion market identifier.
            direction: "YES" or "NO".
            stake_usd: USD stake after fee deduction.
            expected_price: Price at time of signal.
            slippage_pct: Maximum tolerated slippage (default 2 %).

        Returns:
            Bet ID string.
        """
        if self.paper_mode:
            return await self._paper_place_bet(
                market_id, direction, stake_usd, expected_price, slippage_pct
            )

        # TODO: Live bet via Opinion API
        # payload = {
        #     "market_id": market_id,
        #     "direction": direction.lower(),
        #     "stake_usd": stake_usd,
        #     "max_price": str(expected_price * Decimal(str(1 + slippage_pct))),
        # }
        # async with self._session.post(f"{OPINION_API_BASE}/bets", json=payload) as resp:
        #     resp.raise_for_status()
        #     data = await resp.json()
        #     bet_id = data["bet_id"]
        #     self._log.info("place_bet.filled", bet_id=bet_id, fill_price=data.get("fill_price"))
        #     return bet_id
        raise NotImplementedError("Live Opinion bet placement not yet implemented")

    async def _paper_place_bet(
        self,
        market_id: str,
        direction: str,
        stake_usd: float,
        expected_price: Decimal,
        slippage_pct: float,
    ) -> str:
        """Simulate a paper-mode bet with realistic slippage."""
        # Random slippage within ±slippage_pct
        slippage = Decimal(str(random.uniform(-slippage_pct, slippage_pct)))
        fill_price = max(Decimal("0.01"), min(Decimal("0.99"), expected_price + slippage))

        shares = stake_usd / float(fill_price)
        bet_id = f"paper-opinion-{uuid.uuid4().hex[:12]}"
        ts = time.time()

        self._paper_bets[bet_id] = {
            "bet_id": bet_id,
            "market_id": market_id,
            "direction": direction,
            "requested_price": str(expected_price),
            "fill_price": str(fill_price),
            "stake_usd": stake_usd,
            "shares": shares,
            "status": "OPEN",
            "outcome": None,
            "payout_usd": None,
            "created_at": ts,
        }
        self._paper_balance -= stake_usd

        self._log.info(
            "place_bet.paper_filled",
            bet_id=bet_id,
            fill_price=str(fill_price),
            slippage=f"{float(slippage)*100:.3f}%",
            shares=f"{shares:.4f}",
        )
        return bet_id

    # ------------------------------------------------------------------
    # Status & balance
    # ------------------------------------------------------------------

    async def get_bet_status(self, bet_id: str) -> dict:
        """Return status dict for a bet.

        Args:
            bet_id: The bet ID returned from place_bet/place_order.

        Returns:
            Dict with keys: bet_id, status, fill_price, stake_usd, outcome, payout_usd.
        """
        if self.paper_mode:
            if bet_id not in self._paper_bets:
                return {"bet_id": bet_id, "status": "NOT_FOUND"}
            return dict(self._paper_bets[bet_id])

        # TODO: Live bet status
        # async with self._session.get(f"{OPINION_API_BASE}/bets/{bet_id}") as resp:
        #     resp.raise_for_status()
        #     return await resp.json()
        raise NotImplementedError("Live get_bet_status not yet implemented")

    async def get_balance(self) -> float:
        """Return current USD balance.

        Paper mode returns simulated tracked balance.
        """
        if self.paper_mode:
            self._log.debug("get_balance.paper", balance=self._paper_balance)
            return self._paper_balance

        # TODO: Live balance fetch
        # async with self._session.get(f"{OPINION_API_BASE}/account/balance") as resp:
        #     resp.raise_for_status()
        #     data = await resp.json()
        #     return float(data["balance_usd"])
        raise NotImplementedError("Live get_balance not yet implemented")
