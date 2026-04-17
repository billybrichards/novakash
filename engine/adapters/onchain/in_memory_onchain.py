"""In-memory on-chain query — implements ``OnChainTxQueryPort``.

Returns empty outflow list. Used as safe default in the composition root
until the real Polygonscan / web3 adapter lands. With this default the
wallet-delta classifier will tag any wallet-balance decrease as DRIFT
(no matching tx) — the loud classification — so real outflows never get
silently miscategorised.
"""
from __future__ import annotations

from domain.alert_values import OutflowTx
from use_cases.ports.onchain import OnChainTxQueryPort


class InMemoryOnChainQuery(OnChainTxQueryPort):
    def __init__(self) -> None:
        self._txs: list[OutflowTx] = []
        self._latest_block = 0

    def preload(self, txs: list[OutflowTx], latest_block: int = 0) -> None:
        self._txs = list(txs)
        self._latest_block = latest_block

    async def get_outflows_since(
        self,
        wallet: str,
        since_block: int,
    ) -> list[OutflowTx]:
        return [t for t in self._txs if t.block_number >= since_block]

    async def get_latest_block(self) -> int:
        return self._latest_block
