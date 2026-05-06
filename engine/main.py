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

# Apply py-clob-client-v2 compat patches BEFORE any module that imports
# the SDK runs. Currently wraps ``ClobClient.get_order_book`` so callers
# keep receiving the v1-shaped ``OrderBookSummary`` dataclass instead of
# v2's raw dict (Polymarket changed the return type during the V1->V2
# migration on 2026-04-28). See engine/poly_clob_v2_compat.py for detail.
import poly_clob_v2_compat  # noqa: F401,E402

from config.settings import get_settings
settings = get_settings()
from config.logging import configure_logging
from infrastructure.composition import CompositionRoot
from infrastructure.runtime import EngineRuntime


async def main() -> None:
    configure_logging()
    log = structlog.get_logger(__name__)

    # ── Bound the asyncio default ThreadPoolExecutor (audit #371) ──────────
    # Default is 8 workers (cpu_count or floor 8). When all 8 park on
    # blocking SDK calls (Polymarket data-api / CLOB), the asyncio loop
    # starves and the engine deadlocks. Bumping to 24 gives ample headroom
    # for DB ops, redeemer threads, and other ``asyncio.to_thread`` callers
    # to coexist with the dedicated Polymarket SDK pool. The executor is
    # owned by the running loop and reaped on shutdown.
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    default_executor = ThreadPoolExecutor(
        max_workers=24,
        thread_name_prefix="engine-default",
    )
    loop.set_default_executor(default_executor)
    log.info(
        "engine.threadpool_configured",
        max_workers=24,
        thread_name_prefix="engine-default",
    )

    log.info("engine.starting", paper_mode=settings.paper_mode)

    root = CompositionRoot(settings=settings)
    await EngineRuntime(root).run()


if __name__ == "__main__":
    asyncio.run(main())
