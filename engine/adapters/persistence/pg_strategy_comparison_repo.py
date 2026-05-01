"""Adapter: PgStrategyComparisonRepo — Postgres implementation.

Writes computed `StrategyComparison` snapshots to the `strategy_comparison`
table. Idempotent via PK; same snapshot_at + cell key is a no-op.
"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from engine.domain.strategy_comparison.entities import StrategyComparison


class PgStrategyComparisonRepo:
    """Implements StrategyComparisonRepoPort."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def save(self, snapshot: Sequence[StrategyComparison]) -> int:
        """INSERT INTO strategy_comparison (...) VALUES (...) ON CONFLICT DO NOTHING.

        NOT IMPLEMENTED — design skeleton.
        """
        raise NotImplementedError("design skeleton — see docs/architecture/")

    async def prune_older_than_days(self, days: int) -> int:
        """DELETE FROM strategy_comparison WHERE snapshot_at < NOW() - INTERVAL ':days days'.

        NOT IMPLEMENTED — design skeleton.
        """
        raise NotImplementedError("design skeleton — see docs/architecture/")
