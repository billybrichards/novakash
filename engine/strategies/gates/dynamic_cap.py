"""DynamicCapGate -- sets entry cap from confidence level.

Always passes. Attaches entry_cap to gate data for the registry to use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class DynamicCapGate(Gate):
    """Compute dynamic entry cap based on confidence distance.

    Higher confidence -> willing to pay more (higher cap).
    default_cap is used when confidence data is unavailable.
    """

    def __init__(self, default_cap: float = 0.65):
        self._default_cap = default_cap

    @property
    def name(self) -> str:
        return "dynamic_cap"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        dist = surface.poly_confidence_distance
        if dist is None and surface.v2_probability_up is not None:
            dist = abs(surface.v2_probability_up - 0.5)

        if dist is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"no confidence data, using default_cap={self._default_cap}",
                data={"entry_cap": self._default_cap},
            )

        # Scale cap: 0.55 base + 0.3 * distance (capped at 0.85)
        cap = min(0.55 + (dist * 0.3 / 0.5), 0.85)
        cap = max(cap, self._default_cap)

        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=f"cap={cap:.3f} from dist={dist:.3f}",
            data={"entry_cap": cap, "distance": dist},
        )
