"""Scheduler: strategy_comparison_loop.

asyncio task started from `engine/infrastructure/composition.py` — runs every
`interval` seconds, calls `ComputeStrategyComparison.execute()`, persists via
`StrategyComparisonRepoPort.save()`. Crash-isolated from trading hot path.

Cost estimate (see docs/architecture/2026-05-01-strategy-comparison-system.md):
~3-6s per tick, every 300s = ~2% engine CPU sustained.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_PRUNE_RETENTION_DAYS = 30


async def strategy_comparison_loop(
    *,
    use_case,
    repo,
    interval: int = DEFAULT_INTERVAL_SECONDS,
    prune_every_ticks: int = 288,  # ~once per day at 5-min cadence
    prune_retention_days: int = DEFAULT_PRUNE_RETENTION_DAYS,
) -> None:
    """Long-running coroutine — call via asyncio.create_task in composition.

    NOT IMPLEMENTED — design skeleton.

    Plan:
      while True:
          try:
              snapshot = await use_case.execute(now=datetime.now(timezone.utc))
              await repo.save(snapshot)
          except asyncio.CancelledError:
              raise
          except Exception:
              log.exception("strategy_comparison.tick_failed")
          tick += 1
          if tick % prune_every_ticks == 0:
              try:
                  await repo.prune_older_than_days(prune_retention_days)
              except Exception:
                  log.exception("strategy_comparison.prune_failed")
          await asyncio.sleep(interval)
    """
    raise NotImplementedError("design skeleton — see docs/architecture/")
