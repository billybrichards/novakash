"""Port: DecisionsQueryRepoPort.

Read-only adapter contract for querying `strategy_decisions` joined to
`window_snapshots` (for outcome resolution). Adapter impl:
`engine/adapters/persistence/pg_decisions_query_repo.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence


@dataclass(frozen=True)
class DecisionFireRow:
    """One TRADE fire with outcome attached (or None if window unresolved)."""

    strategy_id: str
    asset: str
    timeframe: str
    window_ts: int
    eval_offset: int | None  # seconds-from-window-open
    direction: str  # 'UP' | 'DOWN'
    regime: str | None
    fill_price: float | None
    stake_usd: float | None
    actual_outcome: str | None  # 'UP' | 'DOWN' | None (pending)
    evaluated_at: datetime


class DecisionsQueryRepoPort(Protocol):
    async def fires_with_outcomes(
        self, *, since: datetime, until: datetime
    ) -> Sequence[DecisionFireRow]:
        """Return all TRADE-action fires in the time window with outcome
        resolution joined from window_snapshots."""
        ...
