"""
On-Chain Position Redeemer

Redeems resolved Polymarket positions back to USDC by calling
redeemPositions() on the Conditional Tokens Framework (CTF) contract,
executed through the user's Gnosis Safe proxy wallet.

Flow:
1. Fetch positions from Polymarket data API
2. Check on-chain if each condition has resolved (payoutDenominator > 0)
3. Build redeemPositions calldata for resolved positions with token balance
4. Execute through Gnosis Safe proxy (get nonce → build hash → sign → exec)
5. Report USDC balance change

Based on: LuciferForge/polymarket-settlement-bot
Contracts:
  - CTF: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045 (Polygon)
  - USDC: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174 (Polygon PoS)
  - Gnosis Safe: user's proxy wallet address
"""

from __future__ import annotations

import asyncio
import time
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

GNOSIS_SAFE_ABI = [
    {
        "name": "nonce",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getTransactionHash",
        "type": "function",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "nonce", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "execTransaction",
        "type": "function",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"},
        ],
        "outputs": [{"name": "success", "type": "bool"}],
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
    Redeems resolved Polymarket positions on-chain.

    Requires:
    - Polygon RPC URL (for web3 contract calls)
    - Private key of the EOA that owns the Gnosis Safe proxy
    - Proxy (funder) wallet address on Polymarket

    All operations are async and run in asyncio.to_thread() where needed.
    """

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        proxy_address: str,
        paper_mode: bool = True,
    ) -> None:
        self._rpc_url = rpc_url
        self._private_key = private_key
        self._proxy_address = proxy_address
        self._paper_mode = paper_mode
        self._w3 = None
        self._ctf = None
        self._safe = None
        self._usdc = None
        self._account = None
        self._log = log.bind(component="redeemer", paper_mode=paper_mode)

    async def connect(self) -> None:
        """Initialise web3 connection and contract instances."""
        if self._paper_mode:
            self._log.info("redeemer.skip_connect", reason="paper_mode")
            return

        if not self._rpc_url:
            self._log.warning("redeemer.no_rpc_url")
            return

        try:
            from web3 import Web3
            from eth_account import Account

            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            self._account = Account.from_key(self._private_key)

            self._ctf = self._w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=CTF_ABI,
            )
            self._safe = self._w3.eth.contract(
                address=Web3.to_checksum_address(self._proxy_address),
                abi=GNOSIS_SAFE_ABI,
            )
            self._usdc = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS),
                abi=ERC20_BALANCE_ABI,
            )

            chain_id = self._w3.eth.chain_id
            self._log.info(
                "redeemer.connected",
                chain_id=chain_id,
                proxy=self._proxy_address,
                eoa=self._account.address,
            )
        except Exception as exc:
            self._log.error("redeemer.connect_failed", error=str(exc))

    async def get_usdc_balance(self) -> float:
        """Get USDC balance of the proxy wallet."""
        if not self._w3 or not self._usdc:
            return 0.0
        try:
            from web3 import Web3
            balance_raw = await asyncio.to_thread(
                self._usdc.functions.balanceOf(
                    Web3.to_checksum_address(self._proxy_address)
                ).call
            )
            # USDC has 6 decimals
            return balance_raw / 1e6
        except Exception as exc:
            self._log.debug("redeemer.usdc_balance_error", error=str(exc))
            return 0.0

    async def fetch_redeemable_positions(self) -> list[dict]:
        """
        Fetch positions from Polymarket data API and filter for redeemable ones.

        A position is redeemable when:
        1. curPrice is 0 or 1 (market resolved)
        2. The CTF contract's payoutDenominator > 0 for this condition
        3. The proxy wallet has a token balance for this position

        Returns list of dicts with: conditionId, tokenId, size, outcome, pnl
        """
        if self._paper_mode or not self._w3:
            return []

        try:
            # 1. Fetch positions from data API
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

                # Skip open positions (curPrice between 0.01 and 0.99)
                if 0.01 < cur_price < 0.99:
                    continue

                # Skip zero-size positions
                if size <= 0:
                    continue

                if not condition_id:
                    continue

                # 2. Check on-chain that this condition has resolved
                try:
                    from web3 import Web3
                    cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
                    payout_denom = await asyncio.to_thread(
                        self._ctf.functions.payoutDenominator(cid_bytes).call
                    )
                    if payout_denom == 0:
                        continue  # Not yet resolved on-chain
                except Exception as exc:
                    self._log.debug(
                        "redeemer.payout_check_error",
                        condition=condition_id[:20],
                        error=str(exc),
                    )
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
            )
            return redeemable

        except Exception as exc:
            self._log.error("redeemer.scan_error", error=str(exc))
            return []

    async def redeem_position(self, condition_id: str) -> bool:
        """
        Redeem a single resolved position via Gnosis Safe.

        Steps:
        1. Build redeemPositions calldata (collateral=USDC, indexSets=[1,2])
        2. Get Safe nonce
        3. Get Safe transaction hash
        4. Sign with EOA private key
        5. Execute via execTransaction

        Returns True if redemption succeeded.
        """
        if self._paper_mode or not self._w3 or not self._safe:
            return False

        try:
            from web3 import Web3

            cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            zero_bytes32 = b"\x00" * 32

            # Build redeemPositions calldata
            # indexSets=[1, 2] means redeem both YES (index 0 → set 1) and NO (index 1 → set 2)
            calldata = self._ctf.encodeABI(
                fn_name="redeemPositions",
                args=[
                    Web3.to_checksum_address(USDC_ADDRESS),
                    zero_bytes32,           # parentCollectionId (0 for root)
                    cid_bytes,              # conditionId
                    [1, 2],                 # indexSets: YES=1, NO=2
                ],
            )

            # Get Safe nonce
            nonce = await asyncio.to_thread(self._safe.functions.nonce().call)

            # Build Safe transaction hash
            ctf_checksum = Web3.to_checksum_address(CTF_ADDRESS)
            zero_address = "0x0000000000000000000000000000000000000000"

            tx_hash = await asyncio.to_thread(
                self._safe.functions.getTransactionHash(
                    ctf_checksum,           # to
                    0,                      # value (no ETH)
                    bytes.fromhex(calldata[2:]),  # data (strip 0x)
                    0,                      # operation (CALL)
                    0,                      # safeTxGas
                    0,                      # baseGas
                    0,                      # gasPrice
                    zero_address,           # gasToken
                    zero_address,           # refundReceiver
                    nonce,                  # nonce
                ).call
            )

            # Sign the hash with EOA key
            signed = self._account.signHash(tx_hash)

            # Pack signature: r (32 bytes) + s (32 bytes) + v (1 byte)
            signature = (
                signed.r.to_bytes(32, "big")
                + signed.s.to_bytes(32, "big")
                + signed.v.to_bytes(1, "big")
            )

            # Execute through Safe
            tx = self._safe.functions.execTransaction(
                ctf_checksum,
                0,
                bytes.fromhex(calldata[2:]),
                0,  # CALL
                0, 0, 0,
                zero_address,
                zero_address,
                signature,
            )

            # Build and send the actual transaction from EOA
            gas_estimate = await asyncio.to_thread(
                tx.estimate_gas, {"from": self._account.address}
            )

            built_tx = tx.build_transaction({
                "from": self._account.address,
                "nonce": await asyncio.to_thread(
                    self._w3.eth.get_transaction_count, self._account.address
                ),
                "gas": int(gas_estimate * 1.2),  # 20% buffer
                "gasPrice": await asyncio.to_thread(lambda: self._w3.eth.gas_price),
                "chainId": 137,
            })

            signed_tx = self._account.sign_transaction(built_tx)
            tx_hash_sent = await asyncio.to_thread(
                self._w3.eth.send_raw_transaction, signed_tx.raw_transaction
            )

            # Wait for receipt
            receipt = await asyncio.to_thread(
                self._w3.eth.wait_for_transaction_receipt, tx_hash_sent, timeout=60
            )

            success = receipt.get("status") == 1

            self._log.info(
                "redeemer.redeem_result",
                condition=condition_id[:20] + "...",
                success=success,
                tx_hash=tx_hash_sent.hex(),
                gas_used=receipt.get("gasUsed"),
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
        Scan for redeemable positions and redeem them all.

        Returns summary dict:
        {
            "scanned": int,
            "redeemed": int,
            "failed": int,
            "total_pnl": float,
            "usdc_before": float,
            "usdc_after": float,
        }
        """
        if self._paper_mode:
            return {"scanned": 0, "redeemed": 0, "failed": 0, "total_pnl": 0,
                    "usdc_before": 0, "usdc_after": 0, "paper_mode": True}

        usdc_before = await self.get_usdc_balance()

        positions = await self.fetch_redeemable_positions()
        if not positions:
            return {"scanned": 0, "redeemed": 0, "failed": 0, "total_pnl": 0,
                    "usdc_before": usdc_before, "usdc_after": usdc_before}

        redeemed = 0
        failed = 0
        total_pnl = 0.0

        for pos in positions:
            # Only redeem wins (losses have no USDC to claim)
            if pos["outcome"] != "WIN":
                continue

            success = await self.redeem_position(pos["conditionId"])
            if success:
                redeemed += 1
                total_pnl += pos["pnl"]
            else:
                failed += 1

            # Small delay between redemptions to avoid nonce issues
            await asyncio.sleep(2)

        usdc_after = await self.get_usdc_balance()

        self._log.info(
            "redeemer.sweep_complete",
            redeemed=redeemed,
            failed=failed,
            total_pnl=f"${total_pnl:.2f}",
            usdc_change=f"${usdc_after - usdc_before:.2f}",
        )

        return {
            "scanned": len(positions),
            "redeemed": redeemed,
            "failed": failed,
            "total_pnl": total_pnl,
            "usdc_before": usdc_before,
            "usdc_after": usdc_after,
        }
