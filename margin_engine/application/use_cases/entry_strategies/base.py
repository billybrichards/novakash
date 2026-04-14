"""
Base interface for entry strategies.
"""

from abc import ABC, abstractmethod
from typing import Optional

from margin_engine.domain.entities.position import Position
from margin_engine.adapters.signal.v4_models import V4Snapshot


class EntryStrategy(ABC):
    """Abstract base class for entry strategies."""

    @abstractmethod
    async def evaluate(self, v4: V4Snapshot) -> Optional[Position]:
        """
        Evaluate entry signal and return Position if trade should execute.

        Args:
            v4: V4 snapshot containing all timescale data and context.

        Returns:
            Position if trade should execute, None otherwise.
        """
        pass
