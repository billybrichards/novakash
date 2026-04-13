"""Paper Executor -- simulates fills without real CLOB calls.

Implements OrderExecutionPort for paper/backtest mode.
Fills at entry_cap with small random slippage.

Audit: SP-06 Phase 4.
"""

from __future__ import annotations

import random
import time
import uuid

from domain.ports import OrderExecutionPort
from domain.value_objects import ExecutionResult

# Polymarket binary options fee
FEE_MULTIPLIER = 0.072


class PaperExecutor(OrderExecutionPort):
    """Paper mode: simulate fill at entry_cap with +/-0.5% random slippage."""

    async def execute_order(
        self,
        token_id: str,
        side: str,
        stake_usd: float,
        entry_cap: float,
        price_floor: float,
    ) -> ExecutionResult:
        """Simulate a fill. Always succeeds unless price_floor > entry_cap."""
        start = time.time()

        # Reject obviously invalid inputs
        if entry_cap < price_floor:
            return ExecutionResult(
                success=False,
                failure_reason=f"entry_cap ${entry_cap:.2f} < floor ${price_floor:.2f}",
                stake_usd=stake_usd,
                execution_mode="paper",
                token_id=token_id,
                execution_start=start,
                execution_end=time.time(),
            )

        slippage = random.uniform(-0.005, 0.005)
        fill_price = max(price_floor, min(0.99, entry_cap + slippage))
        shares = stake_usd / fill_price if fill_price > 0 else 0
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        fee = FEE_MULTIPLIER * fill_price * (1.0 - fill_price) * stake_usd

        return ExecutionResult(
            success=True,
            order_id=order_id,
            fill_price=round(fill_price, 4),
            fill_size=round(shares, 2),
            stake_usd=stake_usd,
            fee_usd=round(fee, 4),
            execution_mode="paper",
            fak_attempts=0,
            fak_prices=[],
            token_id=token_id,
            execution_start=start,
            execution_end=time.time(),
        )
