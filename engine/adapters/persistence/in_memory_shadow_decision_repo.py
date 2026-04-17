"""In-memory shadow decision repo — implements ``ShadowDecisionRepository``.

Used for tests and as a safe default in composition root before the PG
migration lands. Stores decisions per window_id in a dict.
"""
from __future__ import annotations

from typing import Optional

from domain.ports import ShadowDecisionRepository
from domain.value_objects import StrategyDecision, WindowKey


class InMemoryShadowDecisionRepository(ShadowDecisionRepository):
    def __init__(self, max_windows: int = 10_000) -> None:
        self._by_window: dict[str, list[StrategyDecision]] = {}
        self._insert_order: list[str] = []
        self._max_windows = max_windows

    async def save(
        self,
        window_key: WindowKey,
        decisions: list[StrategyDecision],
    ) -> None:
        key = window_key.key
        if key not in self._by_window:
            self._insert_order.append(key)
            # Bounded: evict oldest.
            if len(self._insert_order) > self._max_windows:
                evict_key = self._insert_order.pop(0)
                self._by_window.pop(evict_key, None)
        self._by_window[key] = list(decisions)

    async def find_by_window(
        self,
        window_key: WindowKey,
    ) -> list[StrategyDecision]:
        return list(self._by_window.get(window_key.key, []))

    async def find_by_strategy(
        self,
        strategy_id: str,
        since_unix: int,
        limit: int = 1000,
    ) -> list[StrategyDecision]:
        hits: list[StrategyDecision] = []
        for decisions in self._by_window.values():
            for d in decisions:
                if d.strategy_id == strategy_id:
                    hits.append(d)
                    if len(hits) >= limit:
                        return hits
        return hits
