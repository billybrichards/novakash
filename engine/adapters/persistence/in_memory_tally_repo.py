"""In-memory tally repo — implements ``TallyQueryPort``.

Returns zero tallies by default. Used as a composition-root fallback
until the PG tally adapter lands. Tests can override via ``preload()``.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from domain.alert_values import CumulativeTally
from domain.ports import TallyQueryPort


class InMemoryTallyRepo(TallyQueryPort):
    def __init__(self) -> None:
        self._today = CumulativeTally(wins=0, losses=0, pnl_usdc=Decimal("0"))
        self._last_hour = CumulativeTally(wins=0, losses=0, pnl_usdc=Decimal("0"))
        self._session = CumulativeTally(wins=0, losses=0, pnl_usdc=Decimal("0"))
        self._by_strategy: dict[tuple[str, str, str], CumulativeTally] = {}

    def preload(
        self,
        *,
        today: Optional[CumulativeTally] = None,
        last_hour: Optional[CumulativeTally] = None,
        session: Optional[CumulativeTally] = None,
        by_strategy: Optional[dict[tuple[str, str, str], CumulativeTally]] = None,
    ) -> None:
        if today is not None:
            self._today = today
        if last_hour is not None:
            self._last_hour = last_hour
        if session is not None:
            self._session = session
        if by_strategy is not None:
            self._by_strategy = dict(by_strategy)

    async def today(self) -> CumulativeTally:
        return self._today

    async def last_hour(self) -> CumulativeTally:
        return self._last_hour

    async def session(self, since_unix: int) -> CumulativeTally:
        return self._session

    async def today_by_strategy(
        self,
    ) -> dict[tuple[str, str, str], CumulativeTally]:
        return dict(self._by_strategy)

    async def today_combined(
        self, timeframe: Optional[str] = None
    ) -> CumulativeTally:
        if timeframe is None:
            return self._today
        # Sum all strategies for this timeframe.
        wins = 0
        losses = 0
        pnl = Decimal("0")
        for (tf, _sid, _mode), t in self._by_strategy.items():
            if tf == timeframe:
                wins += t.wins
                losses += t.losses
                pnl += t.pnl_usdc
        return CumulativeTally(
            wins=wins, losses=losses, pnl_usdc=pnl, timeframe=timeframe
        )
