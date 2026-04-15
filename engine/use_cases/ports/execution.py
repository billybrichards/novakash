"""Application ports: OrderExecutionPort + TradeRecorderPort.

Belong in the use-case layer — not the domain layer.
Moved from domain/ports.py (V7 clean-architecture fix).
"""
from __future__ import annotations

import abc

from domain.value_objects import ExecutionResult, StakeCalculation, StrategyDecision


class OrderExecutionPort(abc.ABC):
    """Abstracts the order execution strategy (FAK ladder, GTC, paper).

    Different from PolymarketClientPort which is the raw CLOB API.
    This port encapsulates the multi-step execution logic:
    FAK ladder -> RFQ -> GTC fallback.

    Implementations:
      - FAKLadderExecutor: FAK ladder -> RFQ -> GTC fallback (live)
      - PaperExecutor: Simulate fill at cap with small random slippage
    """

    @abc.abstractmethod
    async def execute_order(
        self,
        token_id: str,
        side: str,  # "YES" | "NO"
        stake_usd: float,
        entry_cap: float,
        price_floor: float,
    ) -> ExecutionResult:
        """Execute a single order using the configured strategy.

        Returns an ExecutionResult with fill details or failure info.
        MUST NOT raise -- all exceptions are caught and returned as
        ExecutionResult(success=False, failure_reason=...).
        """
        ...


class TradeRecorderPort(abc.ABC):
    """Records executed trades to the trades table + window_snapshots.

    Extracted from the scattered DB writes in five_min_vpin._execute_trade.
    Consolidates: order_manager.register_order, db.update_window_trade_placed,
    and the metadata dict construction.
    """

    @abc.abstractmethod
    async def record_trade(
        self,
        decision: StrategyDecision,
        result: ExecutionResult,
        stake: StakeCalculation,
    ) -> None:
        """Persist a completed trade to the trades table.

        Fire-and-forget safe -- callers may wrap in asyncio.create_task.
        MUST NOT raise.
        """
        ...
