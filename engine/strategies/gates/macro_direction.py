"""MacroDirectionGate -- checks macro bias alignment with trade direction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class MacroDirectionGate(Gate):
    """Pass if macro direction_gate allows the predicted direction.

    ALLOW_ALL: always pass.
    LONG_ONLY: block DOWN.
    SHORT_ONLY: block UP.
    None: pass by default.
    """

    @property
    def name(self) -> str:
        return "macro_direction"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        gate = surface.v4_macro_direction_gate
        if gate is None or gate == "ALLOW_ALL":
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"macro_gate={gate or 'None'}, pass",
            )

        # Determine direction
        direction = surface.poly_direction
        if direction is None and surface.v2_probability_up is not None:
            direction = "UP" if surface.v2_probability_up > 0.5 else "DOWN"

        if direction is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="no direction to check, pass",
            )

        if gate == "LONG_ONLY" and direction == "DOWN":
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"macro_gate=LONG_ONLY blocks DOWN",
                data={"macro_gate": gate, "direction": direction},
            )

        if gate == "SHORT_ONLY" and direction == "UP":
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"macro_gate=SHORT_ONLY blocks UP",
                data={"macro_gate": gate, "direction": direction},
            )

        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=f"macro_gate={gate} allows {direction}",
            data={"macro_gate": gate, "direction": direction},
        )
