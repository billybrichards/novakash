"""
On-Chain Position Redeemer — Builder Relayer Edition

Redeems resolved Polymarket positions back to USDC by calling
redeemPositions() on the Conditional Tokens Framework (CTF) contract,
executed via the Builder Relayer API (supports Magic.link proxy wallets).

Flow:
1. Fetch positions from Polymarket data API
2. Filter for resolved positions (curPrice <= 0.01 or >= 0.99)
3. Build redeemPositions calldata for each resolved position
4. Submit via Builder Relayer SDK (RelayClient.execute)
5. Poll for CONFIRMED status, report USDC balance change

Replaces the Gnosis Safe execTransaction approach which does NOT work
with Magic.link proxy wallets.

Contracts:
  - CTF:  0x4D97DCd97eC945f40cF65F87097ACe5EA0476045 (Polygon)
  - USDC: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174 (Polygon PoS)
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import aiohttp
import structlog

log = structlog.get_logger(__name__)

# ── Contract Addresses (Polygon Mainnet) ──────────────────────────────────────
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ── Minimal ABIs ──────────────────────────────────────────────────────────────
CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "payoutDenominator",
        "type": "function",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

ERC20_BALANCE_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


class PositionRedeemer:
    """
    Redeems resolved Polymarket positions via the Builder Relayer API.

    Works with Magic.link proxy wallets (unlike direct Gnosis Safe calls).

    Requires:
    - Polygon RPC URL (for web3 contract calls / USDC balance)
    - Private key of the EOA that owns the proxy wallet
    - Proxy (funder) wallet address on Polymarket
    - Builder Relayer API key (BUILDER_KEY env var or builder_key param)

    All operations are async. Synchronous SDK calls are wrapped in asyncio.to_thread().
    """

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        proxy_address: str,
        paper_mode: bool = True,
        builder_key: Optional[str] = None,
    ) -> None:
        self._rpc_url = rpc_url
        self._private_key = private_key
        self._proxy_address = proxy_address
        self._paper_mode = paper_mode
        self._builder_key = builder_key or os.environ.get("BUILDER_API_KEY", "") or os.environ.get("BUILDER_KEY", "")
        self._builder_secret = os.environ.get("BUILDER_SECRET", "")
        self._builder_passphrase = os.environ.get("BUILDER_PASSPHRASE", "")
        self._w3 = None
        self._ctf = None
        self._usdc = None
        self._relay_client = None
        self._log = log.bind(component="redeemer", paper_mode=paper_mode)

    async def connect(self) -> None:
        """Initialise web3 and Builder Relayer client."""
        if self._paper_mode:
            self._log.info("redeemer.skip_connect", reason="paper_mode")
            return

        if not self._rpc_url:
            self._log.warning("redeemer.no_rpc_url")
            return

        try:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))

            self._ctf = self._w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=CTF_ABI,
            )
            self._usdc = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS),
                abi=ERC20_BALANCE_ABI,
            )

            chain_id = self._w3.eth.chain_id
            self._log.info(
                "redeemer.web3_connected",
                chain_id=chain_id,
                proxy=self._proxy_address,
            )
        except Exception as exc:
            self._log.error("redeemer.web3_connect_failed", error=str(exc))

        # Initialise Builder Relayer client
        try:
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig

            if not self._builder_key:
                self._log.warning("redeemer.no_builder_key", hint="Set BUILDER_KEY env var")
                return

            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
            creds = BuilderApiKeyCreds(
                key=self._builder_key,
                secret=self._builder_secret,
                passphrase=self._builder_passphrase,
            )
            config = BuilderConfig(
                local_builder_creds=creds,
            )
            from py_builder_relayer_client.models import RelayerTxType
            self._relay_client = RelayClient(
                relayer_url="https://relayer-v2.polymarket.com",
                chain_id=137,
                private_key=self._private_key,
                builder_config=config,
                relay_tx_type=RelayerTxType.PROXY,
            )
            self._log.info("redeemer.relay_client_ready")
        except Exception as exc:
            self._log.error("redeemer.relay_client_failed", error=str(exc))

    async def get_usdc_balance(self) -> float:
        """Get USDC balance of the proxy wallet (6 decimals)."""
        if not self._w3 or not self._usdc:
            return 0.0
        try:
            from web3 import Web3

            balance_raw = await asyncio.to_thread(
                self._usdc.functions.balanceOf(
                    Web3.to_checksum_address(self._proxy_address)
                ).call
            )
            return balance_raw / 1e6
        except Exception as exc:
            self._log.debug("redeemer.usdc_balance_error", error=str(exc))
            return 0.0

    async def fetch_redeemable_positions(self) -> list[dict]:
        """
        Fetch positions from Polymarket data API and filter for redeemable ones.

        A position is redeemable when curPrice <= 0.01 (loss) or >= 0.99 (win).
        Both wins and losses are redeemed — losses clear the accounting books.

        Returns list of dicts with: conditionId, tokenId, size, outcome, pnl, curPrice
        """
        if self._paper_mode:
            return []

        try:
            funder = self._proxy_address.lower()
            url = f"https://data-api.polymarket.com/positions?user={funder}"
            headers = {"User-Agent": "NovakashEngine/1.0"}

            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        self._log.warning("redeemer.positions_api_error", status=resp.status)
                        return []
                    positions = await resp.json()

            if not positions:
                return []

            redeemable = []

            for p in positions:
                cur_price = float(p.get("curPrice", 0.5))
                size = float(p.get("size", 0))
                condition_id = p.get("conditionId", "")

                # Skip open positions (price still between 0.01 and 0.99)
                if 0.01 < cur_price < 0.99:
                    continue

                # Skip zero-size positions
                if size <= 0:
                    continue

                if not condition_id:
                    continue

                outcome = "WIN" if cur_price >= 0.99 else "LOSS"
                avg_price = float(p.get("avgPrice", 0))
                pnl = (size * cur_price) - (size * avg_price)

                redeemable.append({
                    "conditionId": condition_id,
                    "size": size,
                    "avgPrice": avg_price,
                    "curPrice": cur_price,
                    "outcome": outcome,
                    "pnl": pnl,
                    "tokenId": p.get("tokenId", ""),
                    "asset": p.get("asset", ""),
                })

            self._log.info(
                "redeemer.scan_complete",
                total_positions=len(positions),
                redeemable=len(redeemable),
                wins=sum(1 for p in redeemable if p["outcome"] == "WIN"),
                losses=sum(1 for p in redeemable if p["outcome"] == "LOSS"),
            )
            return redeemable

        except Exception as exc:
            self._log.error("redeemer.scan_error", error=str(exc))
            return []

    async def redeem_position(self, condition_id: str) -> bool:
        """
        Redeem a single resolved position via Builder Relayer.

        Steps:
        1. Build redeemPositions calldata (collateral=USDC, indexSets=[1,2])
        2. Wrap in SafeTransaction
        3. Submit via RelayClient.execute (synchronous → asyncio.to_thread)
        4. Poll until CONFIRMED or FAILED

        Returns True if redemption confirmed.
        """
        if self._paper_mode or not self._relay_client or not self._ctf:
            return False

        try:
            from web3 import Web3
            from py_builder_relayer_client.models import Transaction

            cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            zero_bytes32 = b"\x00" * 32

            # Build redeemPositions calldata
            fn = self._ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                zero_bytes32,   # parentCollectionId (0 for root collection)
                cid_bytes,      # conditionId
                [1, 2],         # indexSets: YES=1, NO=2
            )
            calldata = fn._encode_transaction_data()

            # Use Transaction (SDK handles Safe vs Proxy based on relay_tx_type)
            txn = Transaction(
                to=CTF_ADDRESS,
                data=calldata,
                value="0",
            )

            # Execute via relay (synchronous SDK call — handles signing internally)
            response = await asyncio.to_thread(
                self._relay_client.execute, [txn]
            )

            tx_id = getattr(response, "transactionId", None)
            tx_hash = getattr(response, "transactionHash", None)

            self._log.info(
                "redeemer.submitted",
                condition=condition_id[:20] + "...",
                tx_id=tx_id,
                tx_hash=tx_hash,
            )

            if not tx_id:
                self._log.warning("redeemer.no_tx_id", condition=condition_id[:20])
                return False

            # Poll for confirmation (synchronous poll wrapped in thread)
            confirmed = await asyncio.to_thread(
                self._relay_client.poll_until_state,
                tx_id,
                ["CONFIRMED"],
                "FAILED",
                20,     # max_polls
                3000,   # poll_frequency ms
            )

            success = bool(confirmed)

            self._log.info(
                "redeemer.redeem_result",
                condition=condition_id[:20] + "...",
                success=success,
                tx_hash=tx_hash,
            )

            return success

        except Exception as exc:
            self._log.error(
                "redeemer.redeem_error",
                condition=condition_id[:20] + "...",
                error=str(exc),
            )
            return False

    async def redeem_all(self) -> dict:
        """
        Scan for redeemable positions and redeem them all (wins AND losses).

        Losses are redeemed to clear accounting — they return 0 USDC but
        must be settled on-chain.

        Returns summary dict:
        {
            "scanned": int,
            "redeemed": int,
            "failed": int,
            "wins": int,
            "losses": int,
            "total_pnl": float,
            "usdc_before": float,
            "usdc_after": float,
            "tx_hashes": list[str],
            "paper_mode": bool,
        }
        """
        if self._paper_mode:
            return {
                "scanned": 0,
                "redeemed": 0,
                "failed": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "usdc_before": 0.0,
                "usdc_after": 0.0,
                "tx_hashes": [],
                "paper_mode": True,
            }

        usdc_before = await self.get_usdc_balance()

        positions = await self.fetch_redeemable_positions()
        if not positions:
            return {
                "scanned": 0,
                "redeemed": 0,
                "failed": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "usdc_before": usdc_before,
                "usdc_after": usdc_before,
                "tx_hashes": [],
                "paper_mode": False,
            }

        redeemed = 0
        failed = 0
        wins = 0
        losses = 0
        total_pnl = 0.0
        tx_hashes: list[str] = []

        for pos in positions:
            try:
                success = await self.redeem_position(pos["conditionId"])
                if success:
                    redeemed += 1
                    total_pnl += pos["pnl"]
                    if pos["outcome"] == "WIN":
                        wins += 1
                    else:
                        losses += 1
                else:
                    failed += 1
            except Exception as exc:
                self._log.error(
                    "redeemer.sweep_position_error",
                    condition=pos.get("conditionId", "?")[:20],
                    error=str(exc),
                )
                failed += 1

            # Small delay between redemptions to avoid relay rate limits
            await asyncio.sleep(2)

        usdc_after = await self.get_usdc_balance()

        self._log.info(
            "redeemer.sweep_complete",
            redeemed=redeemed,
            wins=wins,
            losses=losses,
            failed=failed,
            total_pnl=f"${total_pnl:.2f}",
            usdc_change=f"${usdc_after - usdc_before:.2f}",
        )

        return {
            "scanned": len(positions),
            "redeemed": redeemed,
            "failed": failed,
            "wins": wins,
            "losses": losses,
            "total_pnl": total_pnl,
            "usdc_before": usdc_before,
            "usdc_after": usdc_after,
            "tx_hashes": tx_hashes,
            "paper_mode": False,
        }
