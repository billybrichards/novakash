"""
Engine Entry Point

Bootstraps configuration, logging, and the Orchestrator,
then runs until shutdown.
"""

import asyncio
import structlog

from config.settings import settings
from config.logging import configure_logging
from strategies.orchestrator import Orchestrator


async def main() -> None:
    configure_logging()
    log = structlog.get_logger(__name__)
    log.info("engine.starting", paper_mode=settings.paper_mode)

    orch = Orchestrator(settings=settings)
    await orch.run()


if __name__ == "__main__":
    asyncio.run(main())
