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
        min_fill_price: float | None = None,
        max_fill_price: float | None = None,
        strategy_id: str = "",
        window_close_ts: float | None = None,
    ) -> ExecutionResult:
        """Execute a single order using the configured strategy.

        ``strategy_id`` is used by FAKLadderExecutor to scope its in-memory
        GTC dedup so independent strategies can both place their own
        resting orders on the same (token_id, side). Defaults to "" for
        backwards compatibility with legacy tests that don't set it.

        ``min_fill_price`` / ``max_fill_price`` are the band limits that
        the FAK/FOK order should reject fills outside of. For UP/YES
        directions, min_fill_price is the floor (reject fills below it).
        For DOWN/NO directions, max_fill_price is the cap (reject fills above it).

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
        *,
        is_secondary_fill: bool = False,
        parent_trade_id: str | None = None,
    ) -> None:
        """Persist a completed trade to the trades table.

        Fire-and-forget safe -- callers may wrap in asyncio.create_task.
        MUST NOT raise.

        Sub-fill writer (Hub #554, 2026-05-20):
          When ``is_secondary_fill=True`` the recorder MUST persist the
          row with the new ``trades.is_secondary_fill`` column set to
          true and ``trades.parent_trade_id`` set to
          ``parent_trade_id`` (the primary fill's order_id). This
          surfaces the second on-chain fill that lands inside the
          25 s STALE_PLACEHOLDER_TTL window — otherwise it would be
          silently swallowed by the ``window_states`` UNIQUE
          constraint while the wallet sees both fills booked.
        """
        ...
