"""
On-Chain Position Scanner — Multi-RPC consensus reader.

Queries the Conditional Tokens Framework (CTF) contract directly on Polygon
to determine which positions are resolved AND have a non-zero balance on the
proxy wallet.  Uses 2+ RPCs for consensus so we never trust a single cache-lagged
endpoint (lesson from 2026-04-16 incident).

This replaces the Polymarket data-api scan which lags behind on-chain state
and returns stale curPrice values after resolution.

Contracts (Polygon mainnet):
  - CTF:        0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
  - USDC.e:     0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
  - Factory:    0xaB45c5A4B0c941a2F231C04C3f49182e1A254052
  - NegRisk:    0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# ── Contract addresses (Polygon mainnet) ─────────────────────────────────────
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
FACTORY_ADDRESS = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
NEGRISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# ── Minimal ABI for read-only calls ──────────────────────────────────────────
CTF_READ_ABI = [
    {
        "name": "payoutDenominator",
        "type": "function",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]


@dataclass(frozen=True)
class RedeemablePosition:
    """A resolved position with non-zero balance on the proxy wallet."""

    condition_id: str
    yes_balance: int  # raw wei-scale balance (CTF shares, no decimals)
    no_balance: int
    is_resolved: bool  # True iff payoutDenominator > 0


def _compute_position_id(condition_id: str, index_set: int) -> int:
    """Compute the ERC-1155 position ID for a CTF outcome token.

    positionId = keccak256(collateralToken, collectionId)
    collectionId = keccak256(conditionId, indexSet)

    But Polymarket uses parentCollectionId = bytes32(0), so:
      collectionId = keccak256(abi.encodePacked(bytes32(0), conditionId, indexSet))
      positionId   = uint256(keccak256(abi.encodePacked(USDC, collectionId)))
    """
    from eth_abi import encode
    from eth_utils import keccak

    cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
    parent_collection = b"\x00" * 32

    # collectionId = keccak256(parentCollectionId ++ conditionId ++ indexSet)
    collection_id = keccak(
        encode(
            ["bytes32", "bytes32", "uint256"],
            [parent_collection, cid_bytes, index_set],
        )
    )

    # positionId = uint256(keccak256(USDC_address ++ collectionId))
    usdc_bytes = bytes.fromhex(USDC_ADDRESS[2:].lower())
    position_id_bytes = keccak(
        encode(["address", "bytes32"], [usdc_bytes.hex(), collection_id])
    )
    return int.from_bytes(position_id_bytes, "big")


class OnChainPositionScanner:
    """Reads CTF contract state across multiple RPCs for consensus.

    Constructor:
        rpc_urls: list of at least 2 Polygon RPC endpoints
        proxy_address: checksummed address of the Polymarket proxy wallet
        ctf_address: CTF contract address (defaults to mainnet)
    """

    def __init__(
        self,
        rpc_urls: list[str],
        proxy_address: str,
        ctf_address: str = CTF_ADDRESS,
    ) -> None:
        if len(rpc_urls) < 2:
            raise ValueError("OnChainPositionScanner requires at least 2 RPC URLs for consensus")
        self._rpc_urls = rpc_urls
        self._proxy_address = proxy_address
        self._ctf_address = ctf_address
        self._log = log.bind(component="onchain_scanner")

    def _make_web3_and_ctf(self, rpc_url: str):
        """Create a fresh Web3 + CTF contract instance per RPC (avoids cache)."""
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(rpc_url))

        # Polygon POA middleware
        try:
            from web3.middleware import ExtraDataToPOAMiddleware
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        except ImportError:
            try:
                from web3.middleware import geth_poa_middleware
                w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except ImportError:
                pass

        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(self._ctf_address),
            abi=CTF_READ_ABI,
        )
        return w3, ctf

    async def _query_rpc(
        self,
        rpc_url: str,
        condition_id: str,
    ) -> dict:
        """Query a single RPC for payoutDenominator + YES/NO balances.

        Returns: {"payout_denominator": int, "yes_balance": int, "no_balance": int}
        Raises on RPC failure.
        """
        from web3 import Web3

        w3, ctf = self._make_web3_and_ctf(rpc_url)
        proxy = Web3.to_checksum_address(self._proxy_address)
        cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        # payoutDenominator(conditionId)
        payout_denom = await asyncio.to_thread(
            ctf.functions.payoutDenominator(cid_bytes).call
        )

        # Compute position IDs for YES (indexSet=1) and NO (indexSet=2)
        yes_pos_id = _compute_position_id(condition_id, 1)
        no_pos_id = _compute_position_id(condition_id, 2)

        # balanceOf(proxy, positionId)
        yes_balance = await asyncio.to_thread(
            ctf.functions.balanceOf(proxy, yes_pos_id).call
        )
        no_balance = await asyncio.to_thread(
            ctf.functions.balanceOf(proxy, no_pos_id).call
        )

        return {
            "payout_denominator": payout_denom,
            "yes_balance": yes_balance,
            "no_balance": no_balance,
        }

    async def _query_with_consensus(
        self,
        condition_id: str,
    ) -> Optional[dict]:
        """Query 2+ RPCs and return result only when they agree.

        Returns the consensus dict or None if RPCs disagree.
        """
        results = []
        errors = []

        for rpc_url in self._rpc_urls:
            try:
                r = await self._query_rpc(rpc_url, condition_id)
                results.append(r)
            except Exception as exc:
                errors.append(str(exc)[:120])
                self._log.warning(
                    "scanner.rpc_error",
                    rpc=rpc_url[:40],
                    condition=condition_id[:20] + "...",
                    error=str(exc)[:120],
                )

        if len(results) < 2:
            self._log.error(
                "scanner.insufficient_rpcs",
                condition=condition_id[:20] + "...",
                got=len(results),
                errors=errors[:3],
            )
            return None

        # Consensus check: all successful RPCs must agree on payoutDenominator.
        # Balances can drift slightly between RPCs if a tx is in-flight,
        # but payoutDenominator is immutable once set. Use the first result's
        # balances (they're close enough for the "non-zero" check).
        denoms = {r["payout_denominator"] for r in results}
        if len(denoms) > 1:
            self._log.warning(
                "scanner.consensus_mismatch",
                condition=condition_id[:20] + "...",
                denominators=list(denoms),
            )
            return None

        # Use max balances across RPCs (most recent state)
        return {
            "payout_denominator": results[0]["payout_denominator"],
            "yes_balance": max(r["yes_balance"] for r in results),
            "no_balance": max(r["no_balance"] for r in results),
        }

    async def scan_redeemable(
        self,
        condition_ids: list[str],
    ) -> list[RedeemablePosition]:
        """Scan a list of condition IDs and return those that are redeemable.

        A position is redeemable when:
          1. payoutDenominator > 0 (market is resolved on-chain)
          2. proxy wallet holds YES or NO tokens (balance > 0)

        Args:
            condition_ids: list of hex condition ID strings (0x-prefixed or bare)

        Returns:
            list of RedeemablePosition for positions meeting both criteria
        """
        redeemable: list[RedeemablePosition] = []

        for cid in condition_ids:
            try:
                result = await self._query_with_consensus(cid)
                if result is None:
                    continue

                is_resolved = result["payout_denominator"] > 0
                has_balance = result["yes_balance"] > 0 or result["no_balance"] > 0

                if is_resolved and has_balance:
                    redeemable.append(
                        RedeemablePosition(
                            condition_id=cid,
                            yes_balance=result["yes_balance"],
                            no_balance=result["no_balance"],
                            is_resolved=True,
                        )
                    )
                    self._log.info(
                        "scanner.found_redeemable",
                        condition=cid[:20] + "...",
                        yes_balance=result["yes_balance"],
                        no_balance=result["no_balance"],
                        payout_denom=result["payout_denominator"],
                    )
            except Exception as exc:
                self._log.error(
                    "scanner.condition_error",
                    condition=cid[:20] + "...",
                    error=str(exc)[:200],
                )

        self._log.info(
            "scanner.scan_complete",
            scanned=len(condition_ids),
            redeemable=len(redeemable),
        )
        return redeemable
