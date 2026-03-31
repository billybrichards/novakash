"""
Opinion Exchange Execution Client

Opinion (opinion.finance) is a prediction market with lower fees (4%)
compared to Polymarket (7.2%), making it preferred for VPIN cascade
directional bets where we want maximum net payout.

This client handles:
  - Placing directional YES/NO bets via Opinion API
  - Polling bet resolution
  - Paper trading simulation
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Optional
import aiohttp
import structlog

from config.constants import OPINION_CRYPTO_FEE_MULT

log = structlog.get_logger(__name__)

OPINION_BASE_URL = "https://api.opinion.finance/v1"


class OpinionClient:
    """
    Async HTTP client for the Opinion prediction market exchange.

    Opinion offers lower fees (4%) vs Polymarket (7.2%) for crypto markets,
    making it the preferred venue for directional cascade bets.
    """

    def __init__(
        self,
        api_key: str,
        wallet_key: str,
        paper_mode: bool = True,
    ) -> None:
        self.paper_mode = paper_mode
        self._api_key = api_key
        self._wallet_key = wallet_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def connect(self) -> None:
        """Initialise HTTP session and verify API credentials."""
        log.info("opinion.connecting", paper_mode=self.paper_mode)
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
        )
        if not self.paper_mode:
            await self._verify_credentials()
        log.info("opinion.connected")

    async def disconnect(self) -> None:
        """Close HTTP session."""
        if self._session:
            await self._session.close()

    async def _verify_credentials(self) -> None:
        """Check that API key is valid."""
        async with self._session.get(f"{OPINION_BASE_URL}/account") as resp:
            resp.raise_for_status()
            data = await resp.json()
            log.info("opinion.authenticated", wallet=data.get("wallet_address", "?")[:12])

    async def place_bet(
        self,
        market_id: str,
        direction: str,  # "YES" | "NO"
        stake_usd: float,
        expected_price: Decimal,
        slippage_pct: float = 0.02,
    ) -> Optional[str]:
        """
        Place a directional bet on Opinion.

        Args:
            market_id: Opinion market identifier.
            direction: "YES" to bet price goes up, "NO" for down.
            stake_usd: Dollar amount to stake.
            expected_price: Expected fill price (with slippage protection).
            slippage_pct: Maximum acceptable slippage (default 2%).

        Returns:
            Bet ID string if successful, None on failure.
        """
        min_fill = float(expected_price) * (1 - slippage_pct)
        log.info(
            "opinion.bet",
            market=market_id,
            direction=direction,
            stake=stake_usd,
            price=str(expected_price),
            paper=self.paper_mode,
        )

        if self.paper_mode:
            return f"paper-opinion-{market_id[:8]}-{direction.lower()}"

        # TODO: Implement live Opinion API call
        payload = {
            "market_id": market_id,
            "direction": direction,
            "stake_usd": stake_usd,
            "min_fill_price": min_fill,
        }
        async with self._session.post(f"{OPINION_BASE_URL}/bets", json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("bet_id")

    async def get_bet_status(self, bet_id: str) -> dict:
        """Fetch status and resolution of a placed bet."""
        if self.paper_mode:
            return {"status": "OPEN", "resolved": False, "payout_usd": 0.0}

        async with self._session.get(f"{OPINION_BASE_URL}/bets/{bet_id}") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_open_bets(self) -> list[dict]:
        """Return all currently open bets."""
        if self.paper_mode:
            return []

        async with self._session.get(f"{OPINION_BASE_URL}/bets?status=open") as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("bets", [])

    async def get_balance(self) -> float:
        """Return current USDC balance on Opinion."""
        if self.paper_mode:
            return 0.0

        async with self._session.get(f"{OPINION_BASE_URL}/account/balance") as resp:
            resp.raise_for_status()
            data = await resp.json()
            return float(data.get("balance_usd", 0))
