"""Scheduler: strategy_comparison_loop.

asyncio task started from `engine/infrastructure/composition.py` — runs every
`interval` seconds, calls `ComputeStrategyComparison.execute()`, persists via
`StrategyComparisonRepoPort.save()`. Crash-isolated from trading hot path.

Cost estimate (see docs/architecture/2026-05-01-strategy-comparison-system.md):
~3-6s per tick, every 300s = ~2% engine CPU sustained.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import structlog

log = structlog.get_logger(__name__)

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_PRUNE_RETENTION_DAYS = 30

# Indirected so tests can patch without monkeypatching asyncio globally.
_asyncio_sleep = asyncio.sleep


async def strategy_comparison_loop(
    *,
    use_case,
    repo,
    interval: int = DEFAULT_INTERVAL_SECONDS,
    prune_every_ticks: int = 288,  # ~once per day at 5-min cadence
    prune_retention_days: int = DEFAULT_PRUNE_RETENTION_DAYS,
) -> None:
    """Long-running coroutine — call via asyncio.create_task in composition.

    Crash-isolated: exceptions from use_case or repo are caught, logged, and
    the loop continues on the next tick. Only asyncio.CancelledError propagates.
    """
    tick = 0
    while True:
        t0 = time.monotonic()
        try:
            now = datetime.now(timezone.utc)
            snapshot = await use_case.execute(now=now)
            n_rows = await repo.save(snapshot)
            log.info(
                "strategy_comparison.tick_done",
                n_rows=n_rows,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("strategy_comparison.tick_failed")

        tick += 1
        if tick % prune_every_ticks == 0:
            try:
                deleted = await repo.prune_older_than_days(prune_retention_days)
                log.info("strategy_comparison.pruned", deleted=deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("strategy_comparison.prune_failed")

        await _asyncio_sleep(interval)
