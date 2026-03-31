"""
Strategy Orchestrator

Runs all active strategies concurrently, manages data feed lifecycles,
emits a heartbeat every 10 seconds, and handles graceful shutdown on SIGINT/SIGTERM.

Architecture:
  - One asyncio task per feed (Binance WS, CoinGlass, Chainlink, Polymarket WS).
  - MarketAggregator stream() yields snapshots; orchestrator fans them out to strategies.
  - Heartbeat task writes liveness to DB and logs risk state every 10s.
  - Feed monitor task checks staleness and updates DB feed_status flags.
  - On shutdown: cancel all tasks, drain feeds, flush DB writes.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Optional
import structlog

from alerts.telegram import TelegramAlerter
from data.aggregator import MarketAggregator
from data.feeds.binance_ws import BinanceWebSocketFeed
from data.feeds.coinglass_api import CoinGlassAPIFeed
from data.feeds.chainlink_rpc import ChainlinkRPCFeed
from data.feeds.polymarket_ws import PolymarketWebSocketFeed
from data.models import MarketState
from execution.order_manager import OrderManager
from execution.risk_manager import RiskManager
from persistence.db_client import DBClient
from strategies.base import BaseStrategy

log = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL_S = 10
FEED_MONITOR_INTERVAL_S = 15


class Orchestrator:
    """
    Top-level coordinator for the trading engine.

    Usage:
        orch = Orchestrator(...)
        await orch.start()   # connects DB, starts feeds
        await orch.run()     # blocks until shutdown signal
        await orch.stop()    # graceful teardown
    """

    def __init__(
        self,
        strategies: list[BaseStrategy],
        aggregator: MarketAggregator,
        binance_feed: BinanceWebSocketFeed,
        coinglass_feed: CoinGlassAPIFeed,
        chainlink_feed: ChainlinkRPCFeed,
        polymarket_feed: PolymarketWebSocketFeed,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        db_client: DBClient,
        alerter: TelegramAlerter,
    ) -> None:
        self._strategies = strategies
        self._aggregator = aggregator
        self._binance = binance_feed
        self._coinglass = coinglass_feed
        self._chainlink = chainlink_feed
        self._polymarket = polymarket_feed
        self._om = order_manager
        self._rm = risk_manager
        self._db = db_client
        self._alerter = alerter

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

    # ─── Public Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Connect DB, start alerter session, start strategies.

        Called by main.py before run().
        """
        log.info("orchestrator.starting", strategies=[s.name for s in self._strategies])

        # Connect DB
        await self._db.connect()

        # Start alerter session
        await self._alerter.start()

        # Initialise system_state row
        await self._db.update_system_state(engine_status="starting")

        # Start all strategies
        for strategy in self._strategies:
            await strategy.start()

        self._running = True
        self._install_signal_handlers()

        log.info("orchestrator.started")

    async def run(self) -> None:
        """Start all feeds and block until shutdown signal."""
        if not self._running:
            await self.start()

        # Launch all feed tasks
        feed_tasks = [
            asyncio.create_task(self._binance.start(), name="feed_binance"),
            asyncio.create_task(self._coinglass.start(), name="feed_coinglass"),
            asyncio.create_task(self._chainlink.start(), name="feed_chainlink"),
            asyncio.create_task(self._polymarket.start(), name="feed_polymarket"),
        ]

        # Launch orchestration tasks
        orch_tasks = [
            asyncio.create_task(self._market_state_loop(), name="market_state_loop"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._feed_monitor_loop(), name="feed_monitor"),
        ]

        self._tasks = feed_tasks + orch_tasks

        await self._alerter.send_system_alert("🟢 Engine started", level="info")
        await self._db.update_system_state(engine_status="running")

        try:
            await self._shutdown_event.wait()
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Request graceful shutdown from outside (e.g. main.py signal handler)."""
        log.info("orchestrator.stop_requested")
        self.request_shutdown()

    # ─── Internal Loops ───────────────────────────────────────────────────────

    async def _market_state_loop(self) -> None:
        """
        Consume the aggregator stream and fan out MarketState to all strategies.
        """
        async for state in self._aggregator.stream():
            if not self._running:
                break

            await asyncio.gather(
                *[strategy.on_market_state(state) for strategy in self._strategies],
                return_exceptions=True,
            )

    async def _heartbeat_loop(self) -> None:
        """
        Write liveness heartbeat to DB and log risk state every HEARTBEAT_INTERVAL_S.
        """
        while self._running:
            try:
                await self._db.update_heartbeat()

                risk_status = self._rm.get_status()
                open_orders = await self._om.get_open_orders()

                # Persist risk snapshot
                await self._db.update_system_state(
                    engine_status="running",
                    current_balance=risk_status.get("current_bankroll"),
                    peak_balance=risk_status.get("peak_bankroll"),
                    current_drawdown_pct=risk_status.get("drawdown_pct"),
                    active_positions=len(open_orders),
                )

                log.info(
                    "heartbeat",
                    bankroll=risk_status.get("current_bankroll"),
                    drawdown=f"{risk_status.get('drawdown_pct', 0):.2%}",
                    daily_pnl=risk_status.get("daily_pnl"),
                    open_orders=len(open_orders),
                    kill_switch=risk_status.get("kill_switch_active"),
                )
            except Exception as exc:
                log.error("heartbeat.error", error=str(exc))

            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    async def _feed_monitor_loop(self) -> None:
        """
        Periodically update feed connection status flags in the DB.
        """
        while self._running:
            try:
                await self._db.update_feed_status(
                    binance=self._binance.connected,
                    coinglass=self._coinglass.connected,
                    chainlink=self._chainlink.connected,
                    polymarket=self._polymarket.connected,
                )

                log.debug(
                    "feed_monitor",
                    binance=self._binance.connected,
                    coinglass=self._coinglass.connected,
                    chainlink=self._chainlink.connected,
                    polymarket=self._polymarket.connected,
                )
            except Exception as exc:
                log.error("feed_monitor.error", error=str(exc))

            await asyncio.sleep(FEED_MONITOR_INTERVAL_S)

    # ─── Shutdown ─────────────────────────────────────────────────────────────

    async def _shutdown(self) -> None:
        """Gracefully stop all strategies, feeds, and cancel background tasks."""
        log.info("orchestrator.shutting_down")
        self._running = False

        # Stop feeds
        await self._binance.stop()
        await self._coinglass.stop()
        await self._chainlink.stop()
        await self._polymarket.stop()

        # Stop strategies
        for strategy in self._strategies:
            try:
                await strategy.stop()
            except Exception as exc:
                log.error("orchestrator.strategy_stop_error", strategy=strategy.name, error=str(exc))

        # Cancel all background tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Mark engine stopped in DB
        try:
            await self._db.update_system_state(engine_status="stopped")
        except Exception:
            pass

        try:
            await self._alerter.send_system_alert("🔴 Engine stopped", level="warning")
            await self._alerter.close()
        except Exception:
            pass

        await self._db.close()
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
