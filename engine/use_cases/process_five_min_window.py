"""ProcessFiveMinWindowUseCase — decision + execution for a 5-min window signal.

Extracted from EngineRuntime._on_five_min_window (which was ~589 lines).
The runtime callback keeps thin infrastructure glue (logging, TWAP feed,
tick recorder). This class owns the business logic: append window to strategy
queues, evaluate, execute trade if warranted, broadcast to shadow strategies.
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class ProcessFiveMinWindowUseCase:
    """Orchestrates the 5-minute window decision pipeline.

    Injected with:
      - strategy: the live FiveMinStrategy instance (or None in ghost/disabled mode)
      - shadow_strategies: list of ghost strategies that receive the same window
        for evaluation-only (no execution)

    Usage (from runtime callback)::

        await self._process_window_uc.execute(window)
    """

    def __init__(
        self,
        strategy: Any,
        shadow_strategies: list[Any],
    ) -> None:
        self._strategy = strategy
        self._shadow_strategies = shadow_strategies

    async def execute(self, window: Any) -> None:
        """Process one window signal end-to-end.

        Steps:
          1. Append to strategy pending/recent queues (position management)
          2. Evaluate and execute trade if strategy approves
          3. Broadcast to shadow strategies (evaluation-only, no execution)
        """
        if self._strategy is None:
            return None

        # Step 1: queue management (strategy needs recent windows for context)
        self._strategy.append_pending_window(window)
        self._strategy.append_recent_window(window)

        # Step 2: evaluate + execute (strategy owns the decision)
        try:
            await self._strategy.on_window(window)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "process_window.strategy_error",
                asset=getattr(window, "asset", "?"),
                window_ts=getattr(window, "window_ts", 0),
                error=str(exc)[:200],
            )

        # Step 3: shadow evaluation (ghost strategies, no real trades)
        for shadow in self._shadow_strategies:
            try:
                await shadow.on_window(window)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "process_window.shadow_error",
                    strategy=getattr(shadow, "strategy_id", "?"),
                    error=str(exc)[:150],
                )

        return None
