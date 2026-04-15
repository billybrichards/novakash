"""
Engine Entry Point

Bootstraps configuration, logging, and the engine,
then runs until shutdown.
"""
import asyncio
import os
import structlog

# Load .env into os.environ BEFORE any module reads os.environ
try:
    from dotenv import load_dotenv as _ldenv
    _ldenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)
except ImportError:
    pass

from config.settings import settings
from config.logging import configure_logging
from infrastructure.composition import CompositionRoot
from infrastructure.runtime import EngineRuntime


async def main() -> None:
    configure_logging()
    log = structlog.get_logger(__name__)
    log.info("engine.starting", paper_mode=settings.paper_mode)

    root = CompositionRoot(settings=settings)
    await EngineRuntime(root).run()


if __name__ == "__main__":
    asyncio.run(main())
