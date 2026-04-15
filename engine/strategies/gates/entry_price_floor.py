"""EntryPriceFloorGate -- rejects low-conviction trades per direction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

_VALID_DIRECTIONS = ("UP", "DOWN", "both")


class EntryPriceFloorGate(Gate):
    """Reject trades where probability distance from 0.5 is below a per-direction floor.

    params:
      direction: "UP" | "DOWN" | "both"
      min_distance: float  -- minimum |p - 0.5| required to trade

    If direction="UP", only UP trades are filtered (DOWN trades always pass).
    If direction="DOWN", only DOWN trades are filtered (UP trades always pass).
    If direction="both", all trades must clear the floor.

    Uses surface.poly_confidence_distance if available, else |surface.poly_confidence - 0.5|.
    """

    def __init__(self, direction: str, min_distance: float):
        if direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"EntryPriceFloorGate: direction={direction!r} must be one of {_VALID_DIRECTIONS}"
            )
        if not (0.0 < min_distance <= 0.5):
            raise ValueError(
                f"EntryPriceFloorGate: min_distance={min_distance} must be in (0, 0.5]"
            )
        self._direction = direction
        self._min_distance = min_distance

    @property
    def name(self) -> str:
        return "entry_price_floor"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        trade_direction = surface.poly_direction  # "UP" or "DOWN" or None
        confidence = surface.poly_confidence
        distance = surface.poly_confidence_distance

        # Compute distance if not provided directly
        if distance is None and confidence is not None:
            distance = abs(confidence - 0.5)

        if distance is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="no confidence data — gate skipped",
            )

        # Check if this direction is subject to the floor
        if self._direction != "both" and trade_direction != self._direction:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"direction={trade_direction} not subject to {self._direction} floor",
                data={"distance": distance, "direction": trade_direction},
            )

        if distance < self._min_distance:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"dist={distance:.3f} < floor {self._min_distance} for direction={trade_direction}",
                data={"distance": distance, "min_distance": self._min_distance, "direction": trade_direction},
            )

        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=f"dist={distance:.3f} >= floor {self._min_distance}",
            data={"distance": distance, "min_distance": self._min_distance, "direction": trade_direction},
        )
