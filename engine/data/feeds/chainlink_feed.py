"""
Chainlink Multi-Asset Feed — Polygon RPC

Reads BTC/USD, ETH/USD, SOL/USD, XRP/USD from Chainlink aggregator contracts
on Polygon mainnet. Polls every 5 seconds (feeds update every 10-30s on-chain).

Uses POLYGON_RPC_URL from .env — reads go through Montreal's Polygon RPC.

Contract addresses (Polygon mainnet):
  BTC/USD: 0xc907E116054Ad103354f2D350FD2514433D57F6f
  ETH/USD: 0xF9680D99D6C9589e2a93a78A04A279e509205945
  SOL/USD: 0x10C8264C0935b3B9870013e057f330Ff3e9C56dC
  XRP/USD: 0x785ba89291f676b5386652eB12b30cF361020694

Data written to: ticks_chainlink table in Railway PostgreSQL.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Chainlink Aggregator V3 ABI (minimal — latestRoundData + decimals)
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
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Feed config: asset → (contract_address, decimals)
# All Chainlink crypto feeds use 8 decimals on Polygon
FEEDS = {
    "BTC": ("0xc907E116054Ad103354f2D350FD2514433D57F6f", 8),
    "ETH": ("0xF9680D99D6C9589e2a93a78A04A279e509205945", 8),
    "SOL": ("0x10C8264C0935b3B9870013e057f330Ff3e9C56dC", 8),
    "XRP": ("0x785ba89291f676b5386652eB12b30cF361020694", 8),
}

POLL_INTERVAL = 5  # seconds (feeds update every 10-30s, we poll every 5s to catch quickly)
SOURCE = "chainlink_polygon"


class ChainlinkFeed:
    """
    Polls all 4 Chainlink price feeds on Polygon every 5 seconds.

    Writes to ticks_chainlink table. Runs as an async background task.
    Uses the shared asyncpg pool for DB writes (fire-and-forget).

    Attributes:
        connected: True while polling is active and last poll succeeded.
        last_message_at: Timestamp of the most recent successful poll.
    """

    def __init__(self, rpc_url: str, pool) -> None:
        """
        Args:
            rpc_url: Polygon JSON-RPC URL (POLYGON_RPC_URL from .env)
            pool:    asyncpg.Pool from DBClient._pool for ticks_chainlink writes
        """
        self.rpc_url = rpc_url
        self._pool = pool
        self._running = False
        self._connected = False
        self._last_message_at: Optional[datetime] = None
        self._w3 = None
        self._contracts: dict = {}
        # In-memory cache: updated on EVERY poll tick. Keyed by asset name.
        # Read by DataSurfaceManager for zero-I/O delta calculation.
        self.latest_prices: dict[str, float] = {}

    # ─── Public Status ────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self) -> Optional[datetime]:
        return self._last_message_at

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise Web3 contracts and begin polling loop."""
        # Init happens before entering the loop
        init_ok = False
        try:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url))

            # Instantiate all 4 contracts (synchronous Web3 — wrapped in to_thread for async)
            for asset, (addr, _decimals) in FEEDS.items():
                self._contracts[asset] = self._w3.eth.contract(
                    address=Web3.to_checksum_address(addr),
                    abi=AGGREGATOR_ABI,
                )

            log.info(
                "chainlink_feed.starting",
                rpc=self.rpc_url[:40],
                assets=list(FEEDS.keys()),
            )
            init_ok = True
        except ImportError as exc:
            log.error("chainlink_feed.web3_not_installed", error=str(exc))
        except Exception as exc:
            log.error("chainlink_feed.init_failed", error=str(exc))

        if not init_ok:
            return

        self._running = True
        while self._running:
            try:
                await self._poll_all()
                self._connected = True
                self._last_message_at = datetime.now(timezone.utc)
            except Exception as exc:
                log.error("chainlink_feed.poll_error", error=str(exc))
                self._connected = False
            await asyncio.sleep(POLL_INTERVAL)

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        self._connected = False
        log.info("chainlink_feed.stopped")

    # ─── Internal ─────────────────────────────────────────────────────────────

    async def _poll_all(self) -> None:
        """Fetch latest round data from all 4 Chainlink contracts in parallel."""
        tasks = [
            self._poll_asset(asset, contract, FEEDS[asset][1])
            for asset, contract in self._contracts.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        rows = []
        for asset, result in zip(self._contracts.keys(), results):
            if isinstance(result, Exception):
                log.warning("chainlink_feed.asset_error", asset=asset, error=str(result))
                continue
            if result is not None:
                rows.append(result)
                # Update in-memory cache on every poll tick
                # result is (asset, price, round_id, updated_at)
                self.latest_prices[result[0]] = result[1]

        log.debug("chainlink_feed.poll_complete", total_assets=len(self._contracts), rows=len(rows))
        if rows:
            await self._write_rows(rows)

    async def _poll_asset(
        self, asset: str, contract, decimals: int
    ) -> Optional[tuple]:
        """
        Fetch latestRoundData for a single asset.

        Returns:
            (asset, price, round_id, updated_at) tuple or None on error.
        """
        round_id, answer, _started, updated_at, _answered_in = (
            await asyncio.to_thread(contract.functions.latestRoundData().call)
        )
        price = float(answer) / (10 ** decimals)
        log.debug(
            "chainlink_feed.price",
            asset=asset,
            price=f"{price:.4f}",
            round=round_id,
        )
        return (asset, price, int(round_id), int(updated_at))

    async def _write_rows(self, rows: list[tuple]) -> None:
        """Batch INSERT rows into ticks_chainlink.
        
        Args:
            rows: list of (asset, price, round_id, updated_at) tuples
        """
        if not self._pool:
            log.warning("chainlink_feed.no_pool")
            return
        try:
            # Convert round_id to string (uint80 doesn't fit in int64)
            prepared_rows = [
                (row[0], row[1], str(row[2]), row[3], SOURCE)
                for row in rows
            ]
            async with self._pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO ticks_chainlink (ts, asset, price, round_id, updated_at, source)
                    VALUES (NOW(), $1, $2, $3, $4, $5)
                    """,
                    prepared_rows,
                )
            log.info("chainlink_feed.written", rows=len(rows), assets=[r[0] for r in rows])
        except Exception as exc:
            log.error("chainlink_feed.write_error", error=str(exc), rows=len(rows))
