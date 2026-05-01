"""Port: StrategyComparisonRepoPort.

Adapter contract for persisting computed `StrategyComparison` snapshots.
Adapter impl: `engine/adapters/persistence/pg_strategy_comparison_repo.py`.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from domain.strategy_comparison.entities import StrategyComparison


class StrategyComparisonRepoPort(Protocol):
    async def save(self, snapshot: Sequence[StrategyComparison]) -> int:
        """Persist a full snapshot. Returns rows-written count."""
        ...

    async def prune_older_than_days(self, days: int) -> int:
        """Drop snapshots older than `days`. Returns rows-deleted count."""
        ...
