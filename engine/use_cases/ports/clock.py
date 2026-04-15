"""Application port: Clock.

Belongs in the use-case layer — not the domain layer.
Moved from domain/ports.py (V7 clean-architecture fix).
"""
from __future__ import annotations

import abc


class Clock(abc.ABC):
    """Time source -- allows deterministic testing.

    Identical to ``margin_engine.domain.ports.ClockPort`` -- same
    interface intentionally so a future consolidation can use the same
    port.
    """

    @abc.abstractmethod
    def now(self) -> float:
        """Unix epoch seconds."""
        ...
