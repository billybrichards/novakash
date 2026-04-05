"""
Engine Entry Point

Bootstraps configuration, logging, and the Orchestrator,
then runs until shutdown.
"""

import asyncio
import os
import structlog

# Load .env into os.environ BEFORE any module reads os.environ
# (pydantic-settings loads .env into settings.* but NOT os.environ;
#  runtime_config.py uses os.environ.get() directly)
try:
    from dotenv import load_dotenv as _ldenv
    _ldenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)
except ImportError:
    pass

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
