"""Use case: BuildWalletDeltaAlert.

Classifies a wallet balance change and emits a ``WalletDeltaPayload``.

Flow:
  1. Given prior + new balance, query OnChainTxQueryPort for recent outflows.
  2. Pick the most recent matching outflow (by amount ≈ delta).
  3. Classify destination via pure domain function ``classify_wallet_delta``:
     - owner EOA → MANUAL_WITHDRAWAL (INFO tier, audit trail)
     - Polymarket contract → TRADING_FLOW (silent for TG)
     - redeemer addr → REDEMPTION (silent)
     - unknown → UNEXPECTED (TACTICAL, loud — potential exploit)
     - no matching tx → DRIFT (TACTICAL, loud — accounting desync)

Screenshot context: $267.03 → $80.41 overnight was a legit MetaMask
withdrawal, but indistinguishable from an exploit without this classifier.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from domain.alert_logic import classify_wallet_delta
from domain.alert_values import (
    AlertFooter,
    AlertHeader,
    AlertTier,
    LifecyclePhase,
    WalletDelta,
    WalletDeltaKind,
    WalletDeltaPayload,
)
from use_cases.ports import Clock, OnChainTxQueryPort


# Tier per classification — matches proposal TACTICAL/HEARTBEAT/DIAGNOSTIC/INFO.
_KIND_TO_TIER: dict[WalletDeltaKind, AlertTier] = {
    WalletDeltaKind.MANUAL_WITHDRAWAL: AlertTier.INFO,
    WalletDeltaKind.TRADING_FLOW: AlertTier.DIAGNOSTIC,
    WalletDeltaKind.REDEMPTION: AlertTier.DIAGNOSTIC,
    WalletDeltaKind.UNEXPECTED: AlertTier.TACTICAL,
    WalletDeltaKind.DRIFT: AlertTier.TACTICAL,
}


@dataclass
class BuildWalletDeltaAlertInput:
    wallet_addr: str
    prior_balance_usdc: Decimal
    new_balance_usdc: Decimal
    since_block: int
    owner_eoas: frozenset[str]
    poly_contracts: frozenset[str]
    redeemer_addr: Optional[str]
    event_ts_unix: int
    today_realized_pnl_usdc: Optional[Decimal] = None
    amount_tolerance_usdc: Decimal = field(default_factory=lambda: Decimal("0.50"))


class BuildWalletDeltaAlertUseCase:
    def __init__(
        self,
        onchain: OnChainTxQueryPort,
        clock: Clock,
    ) -> None:
        self._onchain = onchain
        self._clock = clock

    async def execute(
        self, inp: BuildWalletDeltaAlertInput
    ) -> Optional[WalletDeltaPayload]:
        delta_amount = inp.new_balance_usdc - inp.prior_balance_usdc
        # Only classify outflows (delta < 0). Inflows handled elsewhere.
        if delta_amount >= 0:
            return None

        outflow_amount = -delta_amount  # positive magnitude

        txs = await self._onchain.get_outflows_since(
            inp.wallet_addr, inp.since_block
        )
        match_tx = self._pick_best_match(
            txs, outflow_amount, inp.amount_tolerance_usdc
        )

        dest = match_tx.to_addr if match_tx else None
        kind = classify_wallet_delta(
            amount_usdc=outflow_amount,
            dest_addr=dest,
            owner_eoas=inp.owner_eoas,
            poly_contracts=inp.poly_contracts,
            redeemer_addr=inp.redeemer_addr,
        )

        # Skip routine trading + redemption flows — user doesn't want spam.
        if kind in {WalletDeltaKind.TRADING_FLOW, WalletDeltaKind.REDEMPTION}:
            return None

        owner_matched: Optional[str] = None
        if kind is WalletDeltaKind.MANUAL_WITHDRAWAL and dest is not None:
            for o in inp.owner_eoas:
                if o.lower() == dest.lower():
                    owner_matched = o
                    break

        delta_obj = WalletDelta(
            kind=kind,
            amount_usdc=delta_amount,
            prior_balance_usdc=inp.prior_balance_usdc,
            new_balance_usdc=inp.new_balance_usdc,
            dest_addr=dest,
            tx_hash=match_tx.tx_hash if match_tx else None,
            realized_trade_pnl_usdc=inp.today_realized_pnl_usdc,
        )

        now_unix = int(self._clock.now())
        return WalletDeltaPayload(
            header=AlertHeader(
                phase=LifecyclePhase.OPS,
                title=self._title_for(kind),
                event_ts_unix=inp.event_ts_unix,
                emit_ts_unix=now_unix,
            ),
            footer=AlertFooter(emit_ts_unix=now_unix),
            tier=_KIND_TO_TIER[kind],
            delta=delta_obj,
            owner_eoa_matched=owner_matched,
            today_realized_pnl_usdc=inp.today_realized_pnl_usdc,
        )

    @staticmethod
    def _pick_best_match(txs, target_amount, tolerance):
        if not txs:
            return None
        best = None
        best_diff = None
        for tx in txs:
            diff = abs(tx.amount_usdc - target_amount)
            if diff <= tolerance:
                if best_diff is None or diff < best_diff:
                    best = tx
                    best_diff = diff
        return best

    @staticmethod
    def _title_for(kind: WalletDeltaKind) -> str:
        return {
            WalletDeltaKind.MANUAL_WITHDRAWAL: "MANUAL WITHDRAWAL",
            WalletDeltaKind.UNEXPECTED: "UNEXPECTED OUTFLOW",
            WalletDeltaKind.DRIFT: "WALLET DRIFT",
            WalletDeltaKind.TRADING_FLOW: "trading flow",
            WalletDeltaKind.REDEMPTION: "redemption",
        }[kind]
