"""
Strategy Orchestrator

Runs all active strategies concurrently, manages data feed lifecycles,
emits a heartbeat every 10 seconds, and handles graceful shutdown on SIGINT/SIGTERM.

Architecture:
  - One asyncio task per strategy (via strategy.on_market_state).
  - A MarketState aggregator publishes snapshots; orchestrator fans them out.
  - Heartbeat task logs liveness and risk state every 10s.
  - On shutdown: cancel all tasks, drain feeds, flush DB writes.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Optional
import structlog

from alerts.telegram import TelegramAlerter
from data.aggregator import MarketStateAggregator
from data.models import MarketState
from execution.order_manager import OrderManager
from execution.risk_manager import RiskManager
from persistence.db_client import DBClient
from strategies.base import BaseStrategy

log = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL_S = 10


class Orchestrator:
    """
    Top-level coordinator for the trading engine.

    Usage:
        orch = Orchestrator(...)
        await orch.run()   # blocks until shutdown signal
    """

    def __init__(
        self,
        strategies: list[BaseStrategy],
        aggregator: MarketStateAggregator,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        db_client: DBClient,
        alerter: TelegramAlerter,
    ) -> None:
        self._strategies = strategies
        self._aggregator = aggregator
        self._om = order_manager
        self._rm = risk_manager
        self._db = db_client
        self._alerter = alerter

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

    # ─── Public Entry Point ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start all components and block until shutdown."""
        log.info("orchestrator.starting", strategies=[s.name for s in self._strategies])

        self._running = True
        self._install_signal_handlers()

        # Start all strategies
        for strategy in self._strategies:
            await strategy.start()

        # Launch concurrent tasks
        self._tasks = [
            asyncio.create_task(self._market_state_loop(), name="market_state_loop"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
        ]

        await self._alerter.send_system_alert("🟢 Engine started", level="info")

        try:
            await self._shutdown_event.wait()
        finally:
            await self._shutdown()

    # ─── Internal Loops ───────────────────────────────────────────────────────

    async def _market_state_loop(self) -> None:
        """
        Poll the aggregator for fresh MarketState snapshots and fan them out
        to all strategies.
        """
        async for state in self._aggregator.stream():
            if not self._running:
                break

            await asyncio.gather(
                *[strategy.on_market_state(state) for strategy in self._strategies],
                return_exceptions=True,
            )

            # Persist market snapshot
            await self._db.update_system_state(
                {
                    "btc_price": str(state.btc_price) if state.btc_price else None,
                    "vpin": state.vpin.value if state.vpin else None,
                    "cascade_state": state.cascade.state if state.cascade else "IDLE",
                    "open_arbs": len(state.arb_opportunities),
                }
            )

    async def _heartbeat_loop(self) -> None:
        """Log liveness and risk snapshot every HEARTBEAT_INTERVAL_S seconds."""
        while self._running:
            risk_status = self._rm.get_status()
            open_orders = await self._om.get_open_orders()

            log.info(
                "heartbeat",
                bankroll=risk_status["current_bankroll"],
                drawdown=f"{risk_status['drawdown_pct']:.2%}",
                daily_pnl=risk_status["daily_pnl"],
                open_orders=len(open_orders),
                kill_switch=risk_status["kill_switch_active"],
            )

            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    # ─── Shutdown ─────────────────────────────────────────────────────────────

    async def _shutdown(self) -> None:
        """Gracefully stop all strategies and cancel background tasks."""
        log.info("orchestrator.shutting_down")
        self._running = False

        # Stop strategies
        for strategy in self._strategies:
            try:
                await strategy.stop()
            except Exception as exc:
                log.error("orchestrator.strategy_stop_error", strategy=strategy.name, error=str(exc))

        # Cancel background tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self._alerter.send_system_alert("🔴 Engine stopped", level="warning")
        log.info("orchestrator.stopped")

    def request_shutdown(self) -> None:
        """Signal the orchestrator to begin graceful shutdown."""
        log.warning("orchestrator.shutdown_requested")
        self._shutdown_event.set()

    def _install_signal_handlers(self) -> None:
        """Register SIGINT/SIGTERM handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()

        def _handle_signal(sig: int) -> None:
            log.warning("orchestrator.signal_received", signal=signal.Signals(sig).name)
            self.request_shutdown()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal, sig)
