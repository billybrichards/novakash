"""
Base Strategy

Abstract base class for all trading strategies. Every strategy must implement
`evaluate` (signal detection) and `execute` (order submission).
"""

from __future__ import annotations

import abc
import asyncio
from typing import Optional
import structlog

from data.models import MarketState
from execution.order_manager import Order, OrderManager
from execution.risk_manager import RiskManager

log = structlog.get_logger(__name__)


class BaseStrategy(abc.ABC):
    """
    Abstract base for prediction market trading strategies.

    Lifecycle:
        1. Orchestrator calls `start()` — one-time initialisation.
        2. Market state updates trigger `on_market_state(state)`.
        3. Internally, strategy calls `evaluate()` to detect signals.
        4. If signal found, `execute()` submits orders via order_manager.
        5. Orchestrator calls `stop()` on shutdown.

    All strategies receive the same MarketState snapshot and share the same
    RiskManager and OrderManager instances.
    """

    def __init__(
        self,
        name: str,
        order_manager: OrderManager,
        risk_manager: RiskManager,
    ) -> None:
        self.name = name
        self._om = order_manager
        self._rm = risk_manager
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Perform any one-time initialisation (subscribe to feeds, etc.)."""
        self._running = True
        log.info("strategy.started", name=self.name)

    async def stop(self) -> None:
        """Gracefully shut down the strategy."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("strategy.stopped", name=self.name)

    # ─── Market State Hook ────────────────────────────────────────────────────

    async def on_market_state(self, state: MarketState) -> None:
        """
        Called by the orchestrator whenever a new MarketState snapshot arrives.

        Default implementation: evaluate for signal, then execute if found.
        Subclasses may override for more complex flows.
        """
        if not self._running:
            return

        signal = await self.evaluate(state)
        if signal:
            await self.execute(state, signal)

    # ─── Abstract Interface ───────────────────────────────────────────────────

    @abc.abstractmethod
    async def evaluate(self, state: MarketState) -> Optional[dict]:
        """
        Analyse the current market state and return a signal dict if a trade
        opportunity is detected, or None if no action should be taken.

        Args:
            state: Latest unified market snapshot.

        Returns:
            Signal dict (strategy-specific schema) or None.
        """
        ...

    @abc.abstractmethod
    async def execute(self, state: MarketState, signal: dict) -> Optional[Order]:
        """
        Submit one or more orders based on the detected signal.

        Must call `self._rm.approve(stake_usd)` before placing any order.
        Must register all placed orders with `self._om.register_order(order)`.

        Args:
            state:  Current market snapshot.
            signal: Signal dict returned by `evaluate`.

        Returns:
            The primary Order placed, or None if execution was blocked.
        """
        ...

    # ─── Helpers ──────────────────────────────────────────────────────────────

    async def _check_risk(self, stake_usd: float) -> tuple[bool, str]:
        """Convenience wrapper around the risk manager gate."""
        return await self._rm.approve(stake_usd, strategy=self.name)

    def __repr__(self) -> str:
        return f"<Strategy name={self.name!r} running={self._running}>"
