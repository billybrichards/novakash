"""Adapter: PgDecisionsQueryRepo — read-only Postgres adapter.

Joins `strategy_decisions` with `window_snapshots` to get outcome and
returns DecisionFireRow records ready for the comparison use case.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from engine.use_cases.ports.decisions_query_repo import DecisionFireRow


class PgDecisionsQueryRepo:
    """Implements DecisionsQueryRepoPort."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def fires_with_outcomes(
        self, *, since: datetime, until: datetime
    ) -> Sequence[DecisionFireRow]:
        """SELECT sd.*, ws.close_price, ws.open_price, sd.metadata_json->>'stake_usd'...

        NOT IMPLEMENTED — design skeleton.

        Filter: action='TRADE' AND executed=true AND evaluated_at IN [since, until).
        """
        raise NotImplementedError("design skeleton — see docs/architecture/")
