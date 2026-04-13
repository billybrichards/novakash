"""DeltaMagnitudeGate -- checks that the primary delta is large enough."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class DeltaMagnitudeGate(Gate):
    """Pass if abs(delta_pct) >= min_threshold."""

    def __init__(self, min_threshold: float):
        self._min = min_threshold

    @property
    def name(self) -> str:
        return "delta_magnitude"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        mag = abs(surface.delta_pct)
        if mag >= self._min:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"|delta|={mag:.6f} >= {self._min:.6f}",
                data={"delta_pct": surface.delta_pct, "magnitude": mag},
            )
        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"|delta|={mag:.6f} < {self._min:.6f}",
            data={"delta_pct": surface.delta_pct, "magnitude": mag},
        )
