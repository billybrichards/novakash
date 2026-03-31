"""
Chainlink Oracle Feed — Polygon RPC

Reads BTC/USD price from the Chainlink aggregator contract on Polygon.
Polls on a configurable interval (default: 10 seconds).

Contract addresses (Polygon mainnet):
  BTC/USD: 0xc907E116054Ad103354f2D350FD2514433D57F6f
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Callable, Awaitable
import structlog
from web3 import AsyncWeb3
from web3.providers.async_rpc import AsyncHTTPProvider

from data.models import ChainlinkPrice

log = structlog.get_logger(__name__)

# Chainlink Aggregator V3 ABI (minimal — only latestRoundData)
AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

BTC_USD_POLYGON = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
DECIMALS = 8  # Chainlink BTC/USD uses 8 decimals
POLL_INTERVAL = 10  # seconds


class ChainlinkRPCFeed:
    """
    Polls Chainlink BTC/USD oracle on Polygon via Web3.

    Provides a trustless price reference to sanity-check
    Binance prices and detect manipulation.
    """

    def __init__(
        self,
        rpc_url: str,
        contract_address: str = BTC_USD_POLYGON,
        poll_interval: int = POLL_INTERVAL,
        on_price: Callable[[ChainlinkPrice], Awaitable[None]] | None = None,
    ) -> None:
        self.rpc_url = rpc_url
        self.contract_address = contract_address
        self.poll_interval = poll_interval
        self._on_price = on_price
        self._running = False
        self._w3: AsyncWeb3 | None = None
        self._contract = None

    async def start(self) -> None:
        """Connect to RPC and start polling."""
        self._w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc_url))
        self._contract = self._w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(self.contract_address),
            abi=AGGREGATOR_ABI,
        )
        self._running = True
        log.info("chainlink.starting", rpc=self.rpc_url[:40])

        while self._running:
            try:
                await self._poll()
            except Exception as exc:
                log.error("chainlink.poll_error", error=str(exc))
            await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False

    async def _poll(self) -> None:
        """Fetch latest round data from the Chainlink contract."""
        round_id, answer, _started, updated_at, _answered_in = (
            await self._contract.functions.latestRoundData().call()
        )
        price = Decimal(answer) / Decimal(10**DECIMALS)
        ts = datetime.utcfromtimestamp(updated_at)

        chainlink_price = ChainlinkPrice(
            feed="BTC/USD",
            price=price,
            round_id=round_id,
            timestamp=ts,
        )

        log.debug("chainlink.price", price=str(price), round=round_id)

        if self._on_price:
            await self._on_price(chainlink_price)
