"""Application port: OnChainTxQueryPort.

Reads outflow transactions for wallet-delta classification. Used by the
classifier to distinguish manual withdrawals, trading flow, redemptions,
and unexpected outflows.

Adapter-facing (Polygonscan / web3 RPC), belongs in the use-case layer.
Phase B of the TG narrative refactor (plans/serialized-drifting-clover.md).
"""
from __future__ import annotations

import abc

from domain.alert_values import OutflowTx


class OnChainTxQueryPort(abc.ABC):
    """Polygon-compatible outflow query for a wallet.

    Implementations:
      - PolygonscanGateway: queries Polygonscan API
      - Web3RpcGateway: queries via Alchemy/Infura RPC
      - InMemory (tests)
    """

    @abc.abstractmethod
    async def get_outflows_since(
        self,
        wallet: str,
        since_block: int,
    ) -> list[OutflowTx]:
        """Return USDC outflows from ``wallet`` since ``since_block``.

        Returns empty list on miss or error — callers treat empty as
        "no tx found" (which itself triggers a DRIFT classification).
        Implementations MUST swallow network errors to empty list.
        """
        ...

    @abc.abstractmethod
    async def get_latest_block(self) -> int:
        """Return the latest finalized block number.

        Used as the ``since_block`` anchor for the next poll. MUST NOT
        return a mempool/pending block.
        """
        ...
