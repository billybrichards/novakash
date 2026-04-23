"""
On-Chain Redemption Transport — Direct factory.proxy() execution.

Submits redeemPositions() transactions directly to the Polygon network
via the Polymarket proxy factory contract, bypassing the Builder Relayer API
entirely. No 100/day quota, no 429s, no data-api lag.

Proven pattern (verified 2026-04-16, $19.96 redeemed in production):
  Inner: CTF.redeemPositions(USDC, bytes32(0), conditionId, [1, 2])
  Outer: factory.proxy([(1, CTF, 0, inner_calldata)])
  Sign with EOA private key, submit via send_raw_transaction.

Contracts (Polygon mainnet):
  - Factory:  0xaB45c5A4B0c941a2F231C04C3f49182e1A254052
  - CTF:      0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
  - USDC.e:   0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# ── Contract addresses ───────────────────────────────────────────────────────
FACTORY_ADDRESS = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ── Gas parameters ───────────────────────────────────────────────────────────
GAS_LIMIT = 450_000
RECEIPT_TIMEOUT = 60  # seconds

# ── USDC Transfer event topic ────────────────────────────────────────────────
# keccak256("Transfer(address,address,uint256)")
_TRANSFER_TOPIC = None  # computed lazily to avoid import-time eth_utils dep


def _get_transfer_topic() -> bytes:
    global _TRANSFER_TOPIC
    if _TRANSFER_TOPIC is None:
        from eth_utils import keccak
        _TRANSFER_TOPIC = keccak(b"Transfer(address,address,uint256)")
    return _TRANSFER_TOPIC


@dataclass(frozen=True)
class RedeemResult:
    """Outcome of a single on-chain redemption attempt."""

    success: bool
    tx_hash: Optional[str] = None
    payout_usdc: float = 0.0
    gas_used: int = 0
    error: Optional[str] = None


class OnChainRedemptionTransport:
    """Submits factory.proxy() redemption transactions directly on Polygon.

    Constructor:
        rpc_url:         Polygon RPC endpoint for tx submission
        private_key:     EOA private key (hex string, 0x-prefixed)
        proxy_address:   checksummed Polymarket proxy wallet address
        factory_address: Polymarket proxy factory (defaults to mainnet)
    """

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        proxy_address: str,
        factory_address: str = FACTORY_ADDRESS,
    ) -> None:
        self._rpc_url = rpc_url
        self._private_key = private_key
        self._proxy_address = proxy_address
        self._factory_address = factory_address

        self._w3 = None
        self._eoa: Optional[str] = None
        self._chain_id: Optional[int] = None
        self._local_nonce: Optional[int] = None

        self._log = log.bind(component="onchain_transport")

    async def connect(self) -> None:
        """Initialise web3, inject POA middleware, derive EOA from private key."""
        from web3 import Web3

        self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))

        # Polygon POA middleware
        try:
            from web3.middleware import ExtraDataToPOAMiddleware
            self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        except ImportError:
            try:
                from web3.middleware import geth_poa_middleware
                self._w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except ImportError:
                pass

        # Derive EOA address from private key
        acct = self._w3.eth.account.from_key(self._private_key)
        self._eoa = acct.address
        self._chain_id = await asyncio.to_thread(lambda: self._w3.eth.chain_id)

        self._log.info(
            "transport.connected",
            eoa=self._eoa,
            chain_id=self._chain_id,
            proxy=self._proxy_address,
        )

    async def _get_nonce(self) -> int:
        """Return the next nonce, lazy-initialising from chain on first call.

        After successful send: caller increments via _increment_nonce().
        On nonce-too-low: caller calls _refetch_nonce() and retries once.
        """
        if self._local_nonce is None:
            self._local_nonce = await asyncio.to_thread(
                self._w3.eth.get_transaction_count, self._eoa, "pending"
            )
            self._log.info("transport.nonce_init", nonce=self._local_nonce)
        return self._local_nonce

    def _increment_nonce(self) -> None:
        """Increment local nonce after a tx was accepted by the mempool."""
        if self._local_nonce is not None:
            self._local_nonce += 1

    async def _refetch_nonce(self) -> int:
        """Re-read nonce from chain (used after nonce-too-low error)."""
        self._local_nonce = await asyncio.to_thread(
            self._w3.eth.get_transaction_count, self._eoa, "pending"
        )
        self._log.info("transport.nonce_refetched", nonce=self._local_nonce)
        return self._local_nonce

    def _build_redeem_calldata(self, condition_id: str) -> bytes:
        """Build the full factory.proxy() calldata wrapping CTF.redeemPositions().

        Inner call: redeemPositions(address, bytes32, bytes32, uint256[])
        Outer call: proxy((uint8, address, uint256, bytes)[])
        """
        from eth_abi import encode
        from eth_utils import keccak

        cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
        zero_bytes32 = b"\x00" * 32
        usdc_addr = bytes.fromhex(USDC_ADDRESS[2:].lower())

        # ── Inner: redeemPositions(collateralToken, parentCollectionId, conditionId, indexSets)
        redeem_selector = keccak(
            b"redeemPositions(address,bytes32,bytes32,uint256[])"
        )[:4]
        redeem_args = encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [
                USDC_ADDRESS,          # collateralToken
                zero_bytes32,          # parentCollectionId (root)
                cid_bytes,             # conditionId
                [1, 2],               # indexSets: YES=1, NO=2
            ],
        )
        inner_calldata = redeem_selector + redeem_args

        # ── Outer: proxy((uint8,address,uint256,bytes)[])
        # Single-element array: (operation=1 (DelegateCall), to=CTF, value=0, data=inner)
        proxy_selector = keccak(
            b"proxy((uint8,address,uint256,bytes)[])"
        )[:4]
        proxy_args = encode(
            ["(uint8,address,uint256,bytes)[]"],
            [
                [(1, CTF_ADDRESS, 0, inner_calldata)],
            ],
        )
        return proxy_selector + proxy_args

    def _parse_usdc_payout(self, receipt) -> float:
        """Extract USDC payout from Transfer event logs in the tx receipt.

        Looks for Transfer(from=proxy, to=proxy, ...) or any Transfer to proxy
        with USDC contract as the emitter.
        """
        from web3 import Web3

        transfer_topic = _get_transfer_topic()
        proxy_padded = bytes.fromhex(
            self._proxy_address[2:].lower().zfill(64)
        )
        usdc_lower = USDC_ADDRESS.lower()

        total_payout = 0.0
        for log_entry in receipt.get("logs", []):
            # Check if this log is from the USDC contract
            log_address = getattr(log_entry, "address", "") or log_entry.get("address", "")
            if log_address.lower() != usdc_lower:
                continue

            topics = log_entry.get("topics", [])
            if not topics:
                continue

            # First topic = Transfer event signature
            first_topic = topics[0]
            if isinstance(first_topic, str):
                first_topic = bytes.fromhex(first_topic.replace("0x", ""))
            elif hasattr(first_topic, "hex"):
                first_topic = bytes(first_topic)

            if first_topic != transfer_topic:
                continue

            # topics[2] = "to" address — check if it's our proxy
            if len(topics) < 3:
                continue
            to_topic = topics[2]
            if isinstance(to_topic, str):
                to_topic = bytes.fromhex(to_topic.replace("0x", ""))
            elif hasattr(to_topic, "hex"):
                to_topic = bytes(to_topic)

            if to_topic != proxy_padded:
                continue

            # data = uint256 amount (USDC has 6 decimals)
            data = log_entry.get("data", b"")
            if isinstance(data, str):
                data = bytes.fromhex(data.replace("0x", ""))
            elif hasattr(data, "hex"):
                data = bytes(data)

            if len(data) >= 32:
                raw_amount = int.from_bytes(data[:32], "big")
                total_payout += raw_amount / 1e6

        return round(total_payout, 6)

    async def _send_tx(self, calldata: bytes, nonce: int) -> dict:
        """Build, sign, and send the EIP-1559 transaction. Returns receipt."""
        from web3 import Web3

        # Fetch gas prices
        max_priority = await asyncio.to_thread(
            lambda: self._w3.eth.max_priority_fee
        )
        base_fee = await asyncio.to_thread(
            lambda: self._w3.eth.get_block("latest")["baseFeePerGas"]
        )
        max_fee = base_fee * 2 + max_priority

        tx = {
            "type": 2,
            "chainId": self._chain_id,
            "from": self._eoa,
            "to": Web3.to_checksum_address(self._factory_address),
            "value": 0,
            "gas": GAS_LIMIT,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority,
            "nonce": nonce,
            "data": calldata,
        }

        signed = self._w3.eth.account.sign_transaction(tx, self._private_key)

        # web3.py version compat: rawTransaction vs raw_transaction
        raw_tx = getattr(signed, "raw_transaction", None) or getattr(
            signed, "rawTransaction", None
        )
        if raw_tx is None:
            raise RuntimeError("signed tx has neither raw_transaction nor rawTransaction")

        tx_hash = await asyncio.to_thread(
            self._w3.eth.send_raw_transaction, raw_tx
        )

        self._log.info(
            "transport.tx_sent",
            tx_hash=tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash),
            nonce=nonce,
        )

        # Wait for receipt
        receipt = await asyncio.to_thread(
            self._w3.eth.wait_for_transaction_receipt,
            tx_hash,
            timeout=RECEIPT_TIMEOUT,
        )
        return receipt

    async def redeem(self, condition_id: str) -> RedeemResult:
        """Submit a factory.proxy() redemption for a single condition ID.

        Builds inner redeemPositions calldata, wraps it in factory.proxy(),
        signs with EOA, and submits to the Polygon network.

        On nonce-too-low: refetches nonce from chain and retries once.
        On "insufficient funds" or RPC error: does NOT increment nonce.

        Returns RedeemResult with success status, tx hash, USDC payout, and gas used.
        """
        if self._w3 is None or self._eoa is None:
            return RedeemResult(
                success=False,
                error="transport not connected — call connect() first",
            )

        calldata = self._build_redeem_calldata(condition_id)

        for attempt in range(2):  # at most 1 retry on nonce-too-low
            nonce = await self._get_nonce()
            try:
                receipt = await self._send_tx(calldata, nonce)

                tx_hash_hex = receipt.get("transactionHash", b"")
                if hasattr(tx_hash_hex, "hex"):
                    tx_hash_hex = tx_hash_hex.hex()
                elif isinstance(tx_hash_hex, bytes):
                    tx_hash_hex = tx_hash_hex.hex()
                tx_hash_str = f"0x{tx_hash_hex}" if not tx_hash_hex.startswith("0x") else tx_hash_hex

                gas_used = receipt.get("gasUsed", 0)
                status = receipt.get("status", 0)

                if status == 1:
                    payout = self._parse_usdc_payout(receipt)
                    self._increment_nonce()
                    self._log.info(
                        "transport.redeem_success",
                        condition=condition_id[:20] + "...",
                        tx_hash=tx_hash_str,
                        payout_usdc=payout,
                        gas_used=gas_used,
                    )
                    return RedeemResult(
                        success=True,
                        tx_hash=tx_hash_str,
                        payout_usdc=payout,
                        gas_used=gas_used,
                    )
                else:
                    # tx was mined but reverted
                    self._increment_nonce()
                    self._log.warning(
                        "transport.redeem_reverted",
                        condition=condition_id[:20] + "...",
                        tx_hash=tx_hash_str,
                        gas_used=gas_used,
                    )
                    return RedeemResult(
                        success=False,
                        tx_hash=tx_hash_str,
                        gas_used=gas_used,
                        error="tx reverted on-chain",
                    )

            except Exception as exc:
                exc_str = str(exc).lower()

                # Nonce-too-low: refetch and retry once
                if "nonce too low" in exc_str and attempt == 0:
                    self._log.warning(
                        "transport.nonce_too_low",
                        condition=condition_id[:20] + "...",
                        old_nonce=nonce,
                    )
                    await self._refetch_nonce()
                    continue

                # Insufficient funds / RPC error: do NOT increment nonce
                if "insufficient funds" in exc_str:
                    self._log.error(
                        "transport.insufficient_funds",
                        condition=condition_id[:20] + "...",
                        error=str(exc)[:200],
                    )
                    return RedeemResult(
                        success=False,
                        error=f"insufficient funds: {str(exc)[:150]}",
                    )

                # Other error
                self._log.error(
                    "transport.send_error",
                    condition=condition_id[:20] + "...",
                    error=str(exc)[:200],
                    attempt=attempt,
                )
                return RedeemResult(
                    success=False,
                    error=str(exc)[:200],
                )

        # Should not reach here, but defensive
        return RedeemResult(success=False, error="exhausted retries")
