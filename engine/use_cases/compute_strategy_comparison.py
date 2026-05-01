"""Use case: ComputeStrategyComparison.

Given a set of decision rows + outcome rows (read via repository ports),
compute one `StrategyComparison` per (strategy, period, t_band, direction,
regime) cell. Pure orchestration — no IO inside this module.

See docs/architecture/2026-05-01-strategy-comparison-system.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from engine.domain.strategy_comparison.entities import StrategyComparison


@dataclass
class ComputeStrategyComparison:
    """Pure use case — receives a query repo, returns a list of rollups.

    Not an asyncio loop — that lives in the scheduler. This is the unit you
    call once to produce one snapshot.
    """

    decisions_query_repo: "DecisionsQueryRepoPort"  # forward-ref string typing OK in skeleton  # noqa: F821

    async def execute(self, *, now: datetime) -> Sequence[StrategyComparison]:
        """Compute one full snapshot of all comparison cells.

        NOT IMPLEMENTED — design skeleton.

        Plan:
          1. For each WindowPeriod, query the decisions_query_repo for fires
             + outcomes in (now - period, now].
          2. Bucket each fire by (strategy_id, t_band, direction, regime).
             For 'all' filters, also accumulate into the aggregate bucket.
          3. For each bucket, compute StrategyMetrics via pnl_math + Wilson.
          4. Return list of StrategyComparison entities.
        """
        raise NotImplementedError("design skeleton — see docs/architecture/")
