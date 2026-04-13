"""ConfidenceGate -- checks probability distance from 0.5."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class ConfidenceGate(Gate):
    """Pass if |probability_up - 0.5| >= min_dist (and <= max_dist if set).

    Uses poly_confidence_distance if available, otherwise computes
    from v2_probability_up.
    """

    def __init__(self, min_dist: float, max_dist: Optional[float] = None):
        self._min = min_dist
        self._max = max_dist

    @property
    def name(self) -> str:
        return "confidence"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        # Prefer poly_confidence_distance, fall back to v2_probability_up
        dist = surface.poly_confidence_distance
        if dist is None and surface.v2_probability_up is not None:
            dist = abs(surface.v2_probability_up - 0.5)

        if dist is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="no confidence distance available",
            )

        if dist < self._min:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"dist={dist:.3f} < min={self._min:.2f}",
                data={"distance": dist},
            )

        if self._max is not None and dist > self._max:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"dist={dist:.3f} > max={self._max:.2f}",
                data={"distance": dist},
            )

        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=f"dist={dist:.3f} in [{self._min:.2f}, {f'{self._max:.2f}' if self._max is not None else 'inf'}]",
            data={"distance": dist},
        )
