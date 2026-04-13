"""Base gate interface and GateResult value object.

All gates in the library inherit from Gate and return GateResult.
Pure Python -- no external dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


@dataclass(frozen=True)
class GateResult:
    """Result of evaluating a single gate against a data surface."""

    passed: bool
    gate_name: str
    reason: str
    data: dict = field(default_factory=dict)


class Gate(ABC):
    """Pure Python gate. No I/O. No external deps.

    Takes a FullDataSurface, returns a GateResult.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this gate (used in logs and skip reasons)."""
        ...

    @abstractmethod
    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        """Evaluate the gate against the data surface.

        Must NOT raise exceptions -- return a failed GateResult instead.
        Must NOT perform I/O -- all data comes from the surface.
        """
        ...
