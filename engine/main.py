"""
BTC Trader Engine — Async Entry Point

Starts all data feeds, signal processors, and the strategy orchestrator.
"""

import asyncio
import signal
import structlog

from config.settings import settings
from strategies.orchestrator import Orchestrator

log = structlog.get_logger(__name__)


async def main() -> None:
    """Bootstrap and run the trading engine."""
    log.info("engine.starting", paper_mode=settings.paper_mode, bankroll=settings.starting_bankroll)

    orchestrator = Orchestrator(settings=settings)

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(sig: signal.Signals) -> None:
        log.info("engine.shutdown_signal", signal=sig.name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    try:
        await orchestrator.start()
        await stop_event.wait()
    finally:
        log.info("engine.stopping")
        await orchestrator.stop()
        log.info("engine.stopped")


if __name__ == "__main__":
    asyncio.run(main())
