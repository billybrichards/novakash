"""DirectionGate -- filters by prediction direction (UP/DOWN/ANY)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class DirectionGate(Gate):
    """Pass if poly_direction matches the configured direction.

    direction="ANY" always passes (no filtering).
    Falls back to v2_probability_up if poly_direction is None.
    """

    def __init__(self, direction: str):
        self._direction = direction.upper()

    @property
    def name(self) -> str:
        return "direction"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        if self._direction == "ANY":
            return GateResult(
                passed=True, gate_name=self.name, reason="direction=ANY, always pass"
            )

        # Determine actual direction
        actual = surface.poly_direction
        if actual is None and surface.v2_probability_up is not None:
            actual = "UP" if surface.v2_probability_up > 0.5 else "DOWN"

        if actual is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="no direction available (poly_direction=None, v2_probability_up=None)",
            )

        if actual.upper() == self._direction:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"direction={actual} matches {self._direction}",
                data={"direction": actual},
            )

        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"direction={actual} != {self._direction}",
            data={"direction": actual},
        )
