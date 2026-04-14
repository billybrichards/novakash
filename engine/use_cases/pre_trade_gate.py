"""PreTradeGate — composable pre-execution safety gate.

Checks run in order (fail-fast):
  1. Window dedup  — DB-backed, survives restart, FAIL-CLOSED
  2. CLOB freshness — price must be non-None and < 30s old
  3. Bankroll sanity — wallet > $5, stake < 25% of wallet
"""
from __future__ import annotations

import time
import structlog
from typing import Optional

from domain.ports import WindowExecutionGuard, WalletBalancePort
from domain.value_objects import PreTradeCheckResult

log = structlog.get_logger()

_CLOB_MAX_AGE_S = 30.0
_MIN_WALLET_USD = 5.0
_MAX_STAKE_PCT = 0.25  # hard cap: stake cannot exceed 25% of wallet


class PreTradeGate:
    """Single composable gate. All 3 checks must pass before any order is sent.

    Checks run in order (fail-fast):
      1. Window dedup  — DB-backed, survives restart, FAIL-CLOSED
      2. CLOB freshness — price must be non-None and < 30s old
      3. Bankroll sanity — wallet > $5, stake < 25% of wallet
    """

    def __init__(
        self,
        guard: WindowExecutionGuard,
        wallet: WalletBalancePort,
    ) -> None:
        self._guard = guard
        self._wallet = wallet

    async def check(
        self,
        strategy_id: str,
        window_ts: int,
        clob_price: Optional[float],
        clob_price_ts: float,
        proposed_stake: float,
    ) -> PreTradeCheckResult:
        # 1. Dedup
        try:
            already = await self._guard.has_executed(strategy_id, window_ts)
        except Exception as exc:
            log.error("pre_trade_gate.dedup_error", error=str(exc)[:120])
            already = True  # FAIL-CLOSED
        if already:
            return PreTradeCheckResult(
                approved=False,
                reason=f"dedup: {strategy_id} already executed window {window_ts}",
            )

        # 2. CLOB freshness
        age_s = time.time() - clob_price_ts
        if clob_price is None:
            return PreTradeCheckResult(
                approved=False,
                reason="clob_stale: price=None",
                clob_price_age_s=age_s,
            )
        if age_s > _CLOB_MAX_AGE_S:
            return PreTradeCheckResult(
                approved=False,
                reason=f"clob_stale: age={age_s:.0f}s > {_CLOB_MAX_AGE_S}s",
                clob_price_age_s=age_s,
            )

        # 3. Live bankroll
        try:
            balance = await self._wallet.get_live_balance()
        except Exception as exc:
            log.error("pre_trade_gate.wallet_error", error=str(exc)[:120])
            balance = 0.0

        if balance < _MIN_WALLET_USD:
            return PreTradeCheckResult(
                approved=False,
                reason=f"bankroll: wallet=${balance:.2f} < ${_MIN_WALLET_USD}",
                live_bankroll=balance,
            )
        if proposed_stake > balance * _MAX_STAKE_PCT:
            return PreTradeCheckResult(
                approved=False,
                reason=(
                    f"bankroll: stake ${proposed_stake:.2f} > "
                    f"{_MAX_STAKE_PCT*100:.0f}% of wallet ${balance:.2f}"
                ),
                live_bankroll=balance,
            )

        return PreTradeCheckResult(
            approved=True,
            reason="ok",
            live_bankroll=balance,
            clob_price_age_s=age_s,
        )

    async def mark_executed(
        self, strategy_id: str, window_ts: int, order_id: str
    ) -> None:
        """Call after a successful order submission."""
        await self._guard.mark_executed(strategy_id, window_ts, order_id)
